import asyncio
import zipfile
import os
import re
import argparse
import shutil
from pyppeteer import launch
from urllib.parse import urlparse


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

    This helps ensure lazy-loaded media is fetched before MHTML capture.
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


async def save_mhtml(url: str, output_file: str):
    """Save webpage as MHTML after forcing assets to load."""
    browser = await launch(
        headless=True,
        args=[
            '--no-sandbox',
            '--disable-dev-shm-usage'
        ]
    )

    page = await browser.newPage()

    # Larger viewport helps trigger responsive/lazy assets
    await page.setViewport({
        "width": 1440,
        "height": 3000
    })

    print(f"Opening {url}")

    await page.goto(
        url,
        waitUntil='networkidle2',
        timeout=120000
    )

    # Scroll to trigger lazy loading
    await auto_scroll(page, max_scrolls=50, scroll_pause=1.5)

    # Scroll back to top
    await page.evaluate("() => window.scrollTo(0, 0)")
    await asyncio.sleep(1)

    # Wait for images/videos to fully load
    await wait_for_assets(page)

    # Small extra delay for pending network requests
    await asyncio.sleep(3)

    print("Capturing MHTML snapshot...")

    mhtml_data = await page._client.send(
        'Page.captureSnapshot',
        {'format': 'mhtml'}
    )

    with open(output_file, 'wb') as f:
        f.write(mhtml_data['data'].encode('utf-8'))

    await browser.close()


def main():
    parser = argparse.ArgumentParser(
        description="Download a webpage as MHTML."
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

    mhtml_filename = f"{base_name}.mhtml"
    zip_filename = f"{base_name}.zip"

    # Create download directory
    download_dir = "download"
    os.makedirs(download_dir, exist_ok=True)

    # Temporary folder for MHTML
    temp_dir = "temp"
    os.makedirs(temp_dir, exist_ok=True)

    mhtml_path = os.path.join(temp_dir, mhtml_filename)

    print(f"Downloading {args.url} → {mhtml_filename}")

    asyncio.run(save_mhtml(args.url, mhtml_path))

    # Create ZIP inside download folder
    zip_path = os.path.join(download_dir, zip_filename)

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.write(mhtml_path, arcname=mhtml_filename)

    # Cleanup temp
    shutil.rmtree(temp_dir, ignore_errors=True)

    print(f"✅ Created {zip_path} (contains {mhtml_filename})")


if __name__ == "__main__":
    main()
