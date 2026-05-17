import asyncio
import zipfile
import os
import re
import argparse
import shutil
from pyppeteer import launch
from urllib.parse import urlparse, urljoin
import aiohttp
import aiofiles
from pathlib import Path
import cssutils
from bs4 import BeautifulSoup

# Enable cssutils logging off
cssutils.log.setLevel(logging.CRITICAL)

def sanitize_filename(name: str) -> str:
    """Remove invalid characters for filenames."""
    return re.sub(r'[\\/*?:"<>|]', "_", name)

async def auto_scroll(page, max_scrolls=50, scroll_pause=1.5):
    """Scroll to the bottom of the page to trigger lazy-loaded assets."""
    last_height = await page.evaluate("() => document.body.scrollHeight")

    for i in range(max_scrolls):
        print(f"Scrolling... ({i + 1}/{max_scrolls})")

        await page.evaluate(
            """
            () => {
                window.scrollTo(0, document.body.scrollHeight);
            }
            """
        )

        await asyncio.sleep(scroll_pause)

        new_height = await page.evaluate("() => document.body.scrollHeight")

        if new_height == last_height:
            print("Reached bottom of page.")
            break

        last_height = new_height

async def wait_for_assets(page, idle_time=3, timeout=30):
    """Wait until images/videos appear fully loaded."""
    print("Waiting for assets to finish loading...")

    js = f"""
    () => new Promise((resolve) => {{
        const start = Date.now();

        function assetsLoaded() {{
            const images = Array.from(document.images);
            const videos = Array.from(document.querySelectorAll('video'));

            const imagesReady = images.every(img =>
                img.complete && img.naturalWidth > 0
            );

            const videosReady = videos.every(video =>
                video.readyState >= 2
            );

            return imagesReady && videosReady;
        }}

        async function check() {{
            while (Date.now() - start < {timeout * 1000}) {{
                if (assetsLoaded()) {{
                    await new Promise(r => setTimeout(r, {idle_time * 1000}));
                    resolve(true);
                    return;
                }}

                await new Promise(r => setTimeout(r, 500));
            }}

            resolve(false);
        }}

        check();
    }})
    """

    await page.evaluate(js)

async def download_asset(session, url, filepath, headers=None, retries=3):
    """Download a single asset with retries and better error handling."""
    for attempt in range(retries):
        try:
            async with session.get(url, headers=headers or {}) as response:
                if response.status == 200:
                    os.makedirs(os.path.dirname(filepath), exist_ok=True)
                    
                    content_type = response.headers.get('content-type', '').lower()
                    
                    # Handle different content types
                    if 'image' in content_type or 'font' in content_type or 'application' in content_type:
                        async with aiofiles.open(filepath, 'wb') as f:
                            await f.write(await response.read())
                    else:
                        # Try to get encoding from response or default to utf-8
                        try:
                            content = await response.text(encoding='utf-8')
                        except:
                            content = await response.text()
                        
                        async with aiofiles.open(filepath, 'w', encoding='utf-8') as f:
                            await f.write(content)
                    return True
                elif response.status == 404:
                    return False  # Don't retry 404s
        except Exception as e:
            if attempt == retries - 1:
                print(f"  ⚠️ Failed after {retries} attempts: {url[:80]}... - {str(e)[:50]}")
            else:
                await asyncio.sleep(1 * (attempt + 1))  # Exponential backoff
    return False

async def process_css_file(css_path, css_content, url_to_local, base_url):
    """Process CSS file to fix URL references."""
    try:
        # Parse CSS
        sheet = cssutils.parseString(css_content)
        
        # Fix url() references
        for rule in sheet:
            if rule.type == rule.STYLE_RULE:
                for property_name in ['background', 'background-image', 'src', 'list-style-image', 'cursor']:
                    if property_name in rule.style:
                        value = rule.style[property_name]
                        # Find all url() references
                        urls = re.findall(r'url\(["\']?([^"\'()]+)["\']?\)', value)
                        for old_url in urls:
                            # Skip data URIs
                            if old_url.startswith('data:'):
                                continue
                            
                            # Make absolute URL
                            if not old_url.startswith(('http://', 'https://', '//')):
                                absolute_url = urljoin(base_url, old_url)
                            else:
                                absolute_url = old_url
                            
                            # Check if we have a local version
                            if absolute_url in url_to_local:
                                new_url = url_to_local[absolute_url]
                                new_value = value.replace(old_url, new_url)
                                rule.style[property_name] = new_value
        
        # Convert back to string
        return sheet.cssText.decode('utf-8') if isinstance(sheet.cssText, bytes) else sheet.cssText
    except Exception as e:
        print(f"  ⚠️ CSS processing error: {e}")
        return css_content

async def extract_critical_css(page):
    """Extract critical CSS and inline styles."""
    critical_css = await page.evaluate('''
        () => {
            let css = '';
            
            // Get all inline styles
            document.querySelectorAll('style').forEach(style => {
                if (style.textContent) {
                    css += style.textContent + '\\n';
                }
            });
            
            // Get computed styles for key elements
            const keySelectors = ['body', 'html', '.header', '.main', '.footer', '.container'];
            keySelectors.forEach(selector => {
                const elements = document.querySelectorAll(selector);
                elements.forEach(el => {
                    const styles = window.getComputedStyle(el);
                    let selectorCSS = selector + ' {\\n';
                    for (let i = 0; i < styles.length; i++) {
                        const prop = styles[i];
                        const value = styles.getPropertyValue(prop);
                        if (value && value !== '') {
                            selectorCSS += `  ${prop}: ${value};\\n`;
                        }
                    }
                    selectorCSS += '}\\n';
                    css += selectorCSS;
                });
            });
            
            return css;
        }
    ''')
    return critical_css

async def get_all_page_resources(page, base_url):
    """Extract all CSS, JS, and image resources from the page."""
    resources = await page.evaluate('''
        () => {
            const resources = {
                css: new Set(),
                js: new Set(),
                images: new Set(),
                fonts: new Set()
            };
            
            // Get all stylesheets
            document.querySelectorAll('link[rel="stylesheet"]').forEach(link => {
                if (link.href) resources.css.add(link.href);
            });
            
            // Get all preloaded CSS
            document.querySelectorAll('link[rel="preload"][as="style"]').forEach(link => {
                if (link.href) resources.css.add(link.href);
            });
            
            // Get inline style background images
            document.querySelectorAll('*').forEach(el => {
                const style = getComputedStyle(el);
                const bgImage = style.backgroundImage;
                if (bgImage && bgImage !== 'none') {
                    const matches = bgImage.match(/url\\(["']?([^"')]+)["']?\\)/g);
                    if (matches) {
                        matches.forEach(match => {
                            let url = match.replace(/url\\(["']?/, '').replace(/["']?\\)/, '');
                            if (url && !url.startsWith('data:')) {
                                // Handle relative URLs
                                if (url.startsWith('//')) {
                                    url = 'https:' + url;
                                }
                                resources.images.add(url);
                            }
                        });
                    }
                }
            });
            
            // Get all scripts
            document.querySelectorAll('script[src]').forEach(script => {
                if (script.src) resources.js.add(script.src);
            });
            
            // Get all images
            document.querySelectorAll('img').forEach(img => {
                if (img.src && !img.src.startsWith('data:')) {
                    resources.images.add(img.src);
                }
            });
            
            // Get picture sources
            document.querySelectorAll('source').forEach(source => {
                if (source.srcset) {
                    const urls = source.srcset.split(',').map(s => s.trim().split(' ')[0]);
                    urls.forEach(url => {
                        if (url && !url.startsWith('data:')) {
                            resources.images.add(url);
                        }
                    });
                }
            });
            
            // Get fonts
            document.querySelectorAll('link[rel="preload"][as="font"]').forEach(link => {
                if (link.href) resources.fonts.add(link.href);
            });
            
            return {
                css: Array.from(resources.css),
                js: Array.from(resources.js),
                images: Array.from(resources.images),
                fonts: Array.from(resources.fonts)
            };
        }
    ''')
    
    # Also parse HTML content for additional resources
    html_content = await page.content()
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Find CSS @import in style tags
    for style in soup.find_all('style'):
        if style.string:
            imports = re.findall(r'@import\s+url\([\'"]?([^\'"()]+)[\'"]?\)', style.string)
            for import_url in imports:
                if import_url and not import_url.startswith('data:'):
                    if import_url.startswith('//'):
                        import_url = 'https:' + import_url
                    resources['css'].append(import_url)
    
    # Remove duplicates
    for key in resources:
        resources[key] = list(set(resources[key]))
    
    return resources

def get_local_filename(url, asset_type, base_url):
    """Generate a local filename for a URL."""
    parsed = urlparse(url)
    path = parsed.path
    
    if not path or path == '/':
        filename = f"index.{asset_type}"
    else:
        filename = os.path.basename(path)
        if not filename:
            filename = f"resource_{hash(url)}.{asset_type}"
        elif '.' not in filename:
            filename = f"{filename}.{asset_type}"
    
    # Clean filename
    name, ext = os.path.splitext(filename)
    name = sanitize_filename(name)[:50]
    
    # Add hash to avoid conflicts
    url_hash = abs(hash(url)) % 10000
    filename = f"{name}_{url_hash}{ext}"
    
    return filename

async def inject_css_fix(html_content, url_to_local, critical_css):
    """Inject CSS fixes into HTML."""
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Create a combined CSS file with all styles
    combined_css = critical_css + '\n'
    
    # Add all external CSS content
    for original_url, local_path in url_to_local.items():
        if local_path.endswith('.css') and original_url in url_to_local:
            css_path = os.path.join('temp_resources', local_path)
            if os.path.exists(css_path):
                try:
                    with open(css_path, 'r', encoding='utf-8') as f:
                        combined_css += f'\n/* From: {original_url} */\n'
                        combined_css += f.read()
                except:
                    pass
    
    # Save combined CSS
    css_dir = os.path.join('temp_resources', 'assets', 'combined')
    os.makedirs(css_dir, exist_ok=True)
    combined_css_path = os.path.join(css_dir, 'combined.css')
    with open(combined_css_path, 'w', encoding='utf-8') as f:
        f.write(combined_css)
    
    # Remove all existing link and style tags
    for link in soup.find_all('link', rel='stylesheet'):
        link.decompose()
    for style in soup.find_all('style'):
        style.decompose()
    
    # Add our combined CSS
    new_style = soup.new_tag('link', rel='stylesheet', href='assets/combined/combined.css')
    if soup.head:
        soup.head.insert(0, new_style)
    
    # Add fallback inline styles
    fallback_style = soup.new_tag('style')
    fallback_style.string = '''
        /* Fallback styles */
        body { visibility: visible !important; }
        img { max-width: 100%; height: auto; }
        * { box-sizing: border-box; }
    '''
    if soup.head:
        soup.head.append(fallback_style)
    
    return str(soup)

async def save_complete_page(url: str, output_dir: str):
    """Save webpage with all assets and fix CSS loading issues."""
    
    browser = await launch(
        headless=True,
        args=[
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--disable-web-security',
            '--disable-features=IsolateOrigins,site-per-process',
            '--disable-javascript-harmony-shipping',  # Better JS compatibility
            '--enable-features=NetworkService,NetworkServiceInProcess'
        ]
    )
    
    page = await browser.newPage()
    
    # Set viewport to a common desktop size
    await page.setViewport({
        "width": 1920,
        "height": 1080
    })
    
    # Set user agent to avoid mobile versions
    await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    
    print(f"🌐 Opening {url}")
    
    try:
        # Navigate to page with better waiting strategy
        response = await page.goto(
            url,
            waitUntil='networkidle0',  # Changed from networkidle2 for more complete loading
            timeout=120000
        )
        
        # Check if page loaded successfully
        if response.status >= 400:
            print(f"⚠️ Page returned status: {response.status}")
        
        # Wait for the page to be interactive
        await page.waitForFunction('document.readyState === "complete"', timeout=10000)
        
        # Scroll to trigger lazy loading
        await auto_scroll(page, max_scrolls=50, scroll_pause=1.5)
        
        # Scroll back to top
        await page.evaluate("() => window.scrollTo(0, 0)")
        await asyncio.sleep(2)
        
        # Wait for all network requests to settle
        await page.waitForFunction('performance.getEntriesByType("resource").length > 0', timeout=5000)
        
        # Wait for assets to load
        await wait_for_assets(page, idle_time=2, timeout=20)
        
        # Extra delay for CSS to apply
        await asyncio.sleep(3)
        
        print("📦 Analyzing page resources...")
        
        # Extract critical CSS before downloading
        critical_css = await extract_critical_css(page)
        
        # Get all resources from the page
        resources = await get_all_page_resources(page, url)
        
        print(f"  Found: {len(resources['css'])} CSS, {len(resources['js'])} JS, {len(resources['images'])} images, {len(resources['fonts'])} fonts")
        
        # Create a temporary directory for resources
        global temp_resources_dir
        temp_resources_dir = output_dir
        
        # Create asset directories
        asset_dir = os.path.join(output_dir, 'assets')
        css_dir = os.path.join(asset_dir, 'css')
        js_dir = os.path.join(asset_dir, 'js')
        img_dir = os.path.join(asset_dir, 'img')
        font_dir = os.path.join(asset_dir, 'fonts')
        
        for d in [css_dir, js_dir, img_dir, font_dir]:
            os.makedirs(d, exist_ok=True)
        
        # Setup mapping for URL to local path
        url_to_local = {}
        
        # Download CSS files first
        print("📥 Downloading CSS files...")
        async with aiohttp.ClientSession() as session:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/css,*/*;q=0.1',
                'Accept-Language': 'en-US,en;q=0.9',
            }
            
            for css_url in resources['css'][:50]:  # Limit to 50 CSS files
                try:
                    # Make URL absolute
                    absolute_url = urljoin(url, css_url)
                    
                    filename = get_local_filename(absolute_url, 'css', url)
                    local_path = os.path.join(css_dir, filename)
                    
                    if await download_asset(session, absolute_url, local_path, headers):
                        # Process downloaded CSS to fix internal URLs
                        async with aiofiles.open(local_path, 'r', encoding='utf-8') as f:
                            css_content = await f.read()
                        
                        processed_css = await process_css_file(local_path, css_content, url_to_local, absolute_url)
                        
                        async with aiofiles.open(local_path, 'w', encoding='utf-8') as f:
                            await f.write(processed_css)
                        
                        url_to_local[absolute_url] = f'assets/css/{filename}'
                        print(f"  ✓ CSS: {filename[:50]}")
                    else:
                        url_to_local[absolute_url] = absolute_url
                except Exception as e:
                    print(f"  ✗ Failed: {css_url[:50]} - {e}")
            
            # Download JS files
            print("📥 Downloading JavaScript files...")
            for js_url in resources['js'][:50]:  # Limit to 50 JS files
                try:
                    absolute_url = urljoin(url, js_url)
                    filename = get_local_filename(absolute_url, 'js', url)
                    local_path = os.path.join(js_dir, filename)
                    if await download_asset(session, absolute_url, local_path, headers):
                        url_to_local[absolute_url] = f'assets/js/{filename}'
                        print(f"  ✓ JS: {filename[:50]}")
                    else:
                        url_to_local[absolute_url] = absolute_url
                except Exception as e:
                    print(f"  ✗ Failed: {js_url[:50]} - {e}")
            
            # Download images
            print("📥 Downloading images...")
            for img_url in resources['images'][:100]:
                try:
                    absolute_url = urljoin(url, img_url)
                    filename = get_local_filename(absolute_url, 'img', url)
                    local_path = os.path.join(img_dir, filename)
                    if await download_asset(session, absolute_url, local_path, headers):
                        url_to_local[absolute_url] = f'assets/img/{filename}'
                except:
                    pass
            
            # Download fonts
            print("📥 Downloading fonts...")
            for font_url in resources['fonts']:
                try:
                    absolute_url = urljoin(url, font_url)
                    filename = get_local_filename(absolute_url, 'font', url)
                    local_path = os.path.join(font_dir, filename)
                    if await download_asset(session, absolute_url, local_path, headers):
                        url_to_local[absolute_url] = f'assets/fonts/{filename}'
                        print(f"  ✓ Font: {filename[:50]}")
                except Exception as e:
                    print(f"  ✗ Font failed: {font_url[:50]}")
        
        # Get the final HTML content
        print("📄 Processing HTML with CSS fixes...")
        html_content = await page.content()
        
        # Replace URLs in HTML with local paths
        for original_url, local_path in url_to_local.items():
            if original_url != local_path:  # Only replace if different
                html_content = html_content.replace(original_url, local_path)
                # Also replace URL-encoded version
                encoded_url = original_url.replace('/', '%2F')
                html_content = html_content.replace(encoded_url, local_path)
        
        # Inject CSS fixes
        html_content = await inject_css_fix(html_content, url_to_local, critical_css)
        
        # Add base tag for relative paths
        base_tag = '<base href="./">'
        if '</head>' in html_content:
            html_content = html_content.replace('</head>', f'{base_tag}\n</head>')
        
        # Ensure proper character encoding
        if '<meta charset=' not in html_content.lower():
            charset_meta = '<meta charset="UTF-8">'
            if '<head>' in html_content:
                html_content = html_content.replace('<head>', f'<head>\n{charset_meta}')
        
        # Save HTML file
        html_path = os.path.join(output_dir, 'index.html')
        async with aiofiles.open(html_path, 'w', encoding='utf-8') as f:
            await f.write(html_content)
        
        print(f"✅ Saved main HTML: {html_path}")
        
        # Create metadata file
        metadata_path = os.path.join(output_dir, 'download_info.txt')
        with open(metadata_path, 'w', encoding='utf-8') as f:
            f.write(f"Original URL: {url}\n")
            f.write(f"Download date: {asyncio.get_event_loop().time()}\n")
            f.write(f"Resources downloaded:\n")
            f.write(f"  - CSS files: {len([k for k in url_to_local if 'assets/css/' in url_to_local[k]])}\n")
            f.write(f"  - JS files: {len([k for k in url_to_local if 'assets/js/' in url_to_local[k]])}\n")
            f.write(f"  - Images: {len([k for k in url_to_local if 'assets/img/' in url_to_local[k]])}\n")
            f.write(f"  - Fonts: {len([k for k in url_to_local if 'assets/fonts/' in url_to_local[k]])}\n")
            f.write(f"\nTroubleshooting:\n")
            f.write(f"- If CSS still doesn't load, check browser console\n")
            f.write(f"- Try opening index.html with a local server\n")
        
        await browser.close()
        return html_path
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        await browser.close()
        raise

async def save_mhtml(url: str, output_file: str):
    """Legacy MHTML save function (kept for backward compatibility)."""
    browser = await launch(
        headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage']
    )
    
    page = await browser.newPage()
    await page.setViewport({"width": 1440, "height": 3000})
    
    print(f"Opening {url}")
    await page.goto(url, waitUntil='networkidle2', timeout=120000)
    
    await auto_scroll(page, max_scrolls=50, scroll_pause=1.5)
    await page.evaluate("() => window.scrollTo(0, 0)")
    await asyncio.sleep(1)
    await wait_for_assets(page)
    await asyncio.sleep(3)
    
    print("Capturing MHTML snapshot...")
    mhtml_data = await page._client.send('Page.captureSnapshot', {'format': 'mhtml'})
    
    with open(output_file, 'wb') as f:
        f.write(mhtml_data['data'].encode('utf-8'))
    
    await browser.close()

def main():
    parser = argparse.ArgumentParser(
        description="Download a webpage with all assets (CSS, JS, images) for offline use"
    )
    
    parser.add_argument(
        "--url",
        required=True,
        help="URL of the page to download"
    )
    
    parser.add_argument(
        "--title",
        help="Optional title for the output file (without extension)"
    )
    
    parser.add_argument(
        "--format",
        choices=['complete', 'mhtml'],
        default='complete',
        help="Format to save: 'complete' (with all assets) or 'mhtml' (single file)"
    )
    
    args = parser.parse_args()
    
    # Determine output filename
    if args.title:
        base_name = sanitize_filename(args.title)
    else:
        parsed = urlparse(args.url)
        path = parsed.path.strip('/').replace('/', '_')
        
        if path:
            base_name = sanitize_filename(path)
        else:
            base_name = sanitize_filename(parsed.netloc)
        
        if not base_name:
            base_name = "webpage"
    
    # Create download directory
    download_dir = "download"
    os.makedirs(download_dir, exist_ok=True)
    
    if args.format == 'complete':
        # Save as complete webpage with assets
        temp_dir = os.path.join("temp", base_name)
        os.makedirs(temp_dir, exist_ok=True)
        
        print(f"📥 Downloading {args.url} as complete webpage...")
        asyncio.run(save_complete_page(args.url, temp_dir))
        
        # Create ZIP of the complete folder
        zip_filename = f"{base_name}.zip"
        zip_path = os.path.join(download_dir, zip_filename)
        
        print(f"📦 Creating ZIP archive...")
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, os.path.dirname(temp_dir))
                    zf.write(file_path, arcname)
        
        # Cleanup temp
        shutil.rmtree(temp_dir, ignore_errors=True)
        
        print(f"✅ Success! Created {zip_path}")
        print(f"   To view: Extract the ZIP and open 'index.html' in a browser")
        print(f"   💡 If CSS doesn't load, try:")
        print(f"      1. Open browser developer tools (F12)")
        print(f"      2. Check Console for errors")
        print(f"      3. Or run a local server: python -m http.server 8000")
        
    else:
        # Legacy MHTML format
        mhtml_filename = f"{base_name}.mhtml"
        zip_filename = f"{base_name}.zip"
        
        temp_dir = "temp"
        os.makedirs(temp_dir, exist_ok=True)
        mhtml_path = os.path.join(temp_dir, mhtml_filename)
        
        print(f"📥 Downloading {args.url} as MHTML...")
        asyncio.run(save_mhtml(args.url, mhtml_path))
        
        zip_path = os.path.join(download_dir, zip_filename)
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.write(mhtml_path, arcname=mhtml_filename)
        
        shutil.rmtree(temp_dir, ignore_errors=True)
        print(f"✅ Created {zip_path} (contains {mhtml_filename})")
        print(f"   Note: MHTML files may not execute JavaScript properly")

if __name__ == "__main__":
    # Install required packages if not present
    try:
        import cssutils
        from bs4 import BeautifulSoup
    except ImportError:
        print("Installing required packages...")
        os.system('pip install cssutils beautifulsoup4 aiohttp aiofiles pyppeteer')
        print("Please run the script again.")
        exit(1)
    
    main()