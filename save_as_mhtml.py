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

def sanitize_filename(name: str) -> str:
    """Remove invalid characters for filenames."""
    return re.sub(r'[\\/*?:"<>|]', "_", name)

async def auto_scroll(page, max_scrolls=50, scroll_pause=1.5):
    """
    Scroll to the bottom of the page to trigger lazy-loaded assets.
    max_scrolls prevents infinite scrolling pages from running forever.
    """
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

        # Stop if page height no longer changes
        if new_height == last_height:
            print("Reached bottom of page.")
            break

        last_height = new_height

async def wait_for_assets(page, idle_time=3, timeout=30):
    """
    Wait until images/videos appear fully loaded.
    This helps ensure lazy-loaded media is fetched before capture.
    """
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

async def download_asset(session, url, filepath, headers=None):
    """Download a single asset (CSS, JS, image) with error handling."""
    try:
        async with session.get(url, headers=headers or {}) as response:
            if response.status == 200:
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                
                # Check if it's binary or text
                content_type = response.headers.get('content-type', '')
                if 'image' in content_type or 'font' in content_type or 'application' in content_type:
                    # Binary file
                    async with aiofiles.open(filepath, 'wb') as f:
                        await f.write(await response.read())
                else:
                    # Text file
                    content = await response.text()
                    async with aiofiles.open(filepath, 'w', encoding='utf-8') as f:
                        await f.write(content)
                return True
    except Exception as e:
        print(f"  ⚠️ Failed to download: {url[:80]}... - {str(e)[:50]}")
    return False

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
            
            // Get inline style background images
            document.querySelectorAll('*').forEach(el => {
                const style = getComputedStyle(el);
                const bgImage = style.backgroundImage;
                if (bgImage && bgImage !== 'none') {
                    const matches = bgImage.match(/url\\(["']?([^"')]+)["']?\\)/g);
                    if (matches) {
                        matches.forEach(match => {
                            const url = match.replace(/url\\(["']?/, '').replace(/["']?\\)/, '');
                            if (url && !url.startsWith('data:')) {
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
    
    # Also get resources from page.content() for any dynamic ones
    html_content = await page.content()
    
    # Find CSS @import and url() in inline styles
    import re
    css_urls = re.findall(r'url\(["\']?([^"\'()]+)["\']?\)', html_content)
    for url in css_urls:
        if url and not url.startswith('data:') and not url.startswith('#'):
            if any(url.endswith(ext) for ext in ['.woff', '.woff2', '.ttf', '.eot']):
                resources['fonts'].append(url)
            elif any(url.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg']):
                resources['images'].append(url)
    
    # Remove duplicates by converting to set and back
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
        # Get the last part of the path
        filename = os.path.basename(path)
        if not filename:
            filename = f"resource.{asset_type}"
    
    # Ensure unique filename by adding hash if needed
    name, ext = os.path.splitext(filename)
    if not ext:
        ext = f".{asset_type}"
    
    # Clean filename
    filename = sanitize_filename(name)[:50] + ext
    
    return filename

async def save_complete_page(url: str, output_dir: str):
    """Save webpage with all assets (CSS, JS, images) for offline use."""
    
    browser = await launch(
        headless=True,
        args=[
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--disable-web-security',  # Allow cross-origin resource loading
            '--disable-features=IsolateOrigins,site-per-process'
        ]
    )
    
    page = await browser.newPage()
    
    # Set larger viewport
    await page.setViewport({
        "width": 1920,
        "height": 1080
    })
    
    print(f"🌐 Opening {url}")
    
    try:
        # Navigate to page
        await page.goto(
            url,
            waitUntil='networkidle2',
            timeout=120000
        )
        
        # Scroll to trigger lazy loading
        await auto_scroll(page, max_scrolls=50, scroll_pause=1.5)
        
        # Scroll back to top
        await page.evaluate("() => window.scrollTo(0, 0)")
        await asyncio.sleep(2)
        
        # Wait for images/videos to fully load
        await wait_for_assets(page)
        
        # Extra delay for dynamic content
        await asyncio.sleep(3)
        
        print("📦 Analyzing page resources...")
        
        # Get all resources from the page
        resources = await get_all_page_resources(page, url)
        
        print(f"  Found: {len(resources['css'])} CSS, {len(resources['js'])} JS, {len(resources['images'])} images, {len(resources['fonts'])} fonts")
        
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
        
        # Download CSS files
        print("📥 Downloading CSS files...")
        async with aiohttp.ClientSession() as session:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            for css_url in resources['css']:
                filename = get_local_filename(css_url, 'css', url)
                local_path = os.path.join(css_dir, filename)
                if await download_asset(session, css_url, local_path, headers):
                    url_to_local[css_url] = f'assets/css/{filename}'
                    print(f"  ✓ CSS: {filename}")
                else:
                    # Keep original URL if download fails
                    url_to_local[css_url] = css_url
            
            # Download JS files
            print("📥 Downloading JavaScript files...")
            for js_url in resources['js']:
                filename = get_local_filename(js_url, 'js', url)
                local_path = os.path.join(js_dir, filename)
                if await download_asset(session, js_url, local_path, headers):
                    url_to_local[js_url] = f'assets/js/{filename}'
                    print(f"  ✓ JS: {filename}")
                else:
                    url_to_local[js_url] = js_url
            
            # Download images
            print("📥 Downloading images...")
            for img_url in resources['images'][:100]:  # Limit to 100 images to avoid overwhelming
                filename = get_local_filename(img_url, 'img', url)
                local_path = os.path.join(img_dir, filename)
                if await download_asset(session, img_url, local_path, headers):
                    url_to_local[img_url] = f'assets/img/{filename}'
                    # Don't print every image to avoid spam
                    # print(f"  ✓ Image: {filename}")
            
            # Download fonts
            print("📥 Downloading fonts...")
            for font_url in resources['fonts']:
                filename = get_local_filename(font_url, 'font', url)
                local_path = os.path.join(font_dir, filename)
                if await download_asset(session, font_url, local_path, headers):
                    url_to_local[font_url] = f'assets/fonts/{filename}'
                    print(f"  ✓ Font: {filename}")
        
        # Get the final HTML content
        print("📄 Processing HTML...")
        html_content = await page.content()
        
        # Replace URLs in HTML with local paths
        for original_url, local_path in url_to_local.items():
            # Replace exact URL matches
            html_content = html_content.replace(original_url, local_path)
            
            # Also replace URL-encoded version
            encoded_url = original_url.replace('/', '%2F')
            html_content = html_content.replace(encoded_url, local_path)
        
        # Add base tag for relative paths
        base_tag = '<base href="./">'
        if '</head>' in html_content:
            html_content = html_content.replace('</head>', f'{base_tag}\n</head>')
        else:
            html_content = base_tag + '\n' + html_content
        
        # Save HTML file
        html_path = os.path.join(output_dir, 'index.html')
        async with aiofiles.open(html_path, 'w', encoding='utf-8') as f:
            await f.write(html_content)
        
        print(f"✅ Saved main HTML: {html_path}")
        
        # Create a metadata file with info about the download
        metadata_path = os.path.join(output_dir, 'download_info.txt')
        with open(metadata_path, 'w', encoding='utf-8') as f:
            f.write(f"Original URL: {url}\n")
            f.write(f"Download date: {asyncio.get_event_loop().time()}\n")
            f.write(f"Resources downloaded:\n")
            f.write(f"  - CSS files: {len(resources['css'])}\n")
            f.write(f"  - JS files: {len(resources['js'])}\n")
            f.write(f"  - Images: {len([k for k in url_to_local if 'img' in url_to_local[k]])}\n")
            f.write(f"  - Fonts: {len(resources['fonts'])}\n")
        
        await browser.close()
        return html_path
        
    except Exception as e:
        print(f"❌ Error: {e}")
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
    main()