import os
import re
import logging
import requests
import urllib.parse
import base64
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from bs4 import BeautifulSoup
import yt_dlp
import concurrent.futures

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
# Enable CORS for all domains to support requests from the frontend client
CORS(app)

# Community-maintained Cobalt API instances (v10 API format: POST /)
COBALT_INSTANCES = [
    "https://cobaltapi.cjs.nz",
    "https://cobaltapi.kittycat.boo",
    "https://rue-cobalt.xenon.zone",
    "https://apicobalt.mgytr.top",
    "https://bergung-api.hoffnungfuerdiezukunft.net",
    "https://nuko-c.meowing.de",
    "https://subito-c.meowing.de"
]

def sanitize_url(url):
    """
    Strips any query parameters inside Python before processing.
    """
    if not url:
        return ""
    url = url.strip()
    if "?" in url:
        url = url.split("?")[0]
    return url

def is_valid_instagram_cdn_media(url):
    """
    Validates if a URL is a direct Instagram CDN media stream URL (photo/video),
    and filters out static media assets, placeholder icons, user profile pictures,
    or default fallback graphic URLs.
    """
    if not url or not isinstance(url, str):
        return False
    
    url_lower = url.lower()
    
    # Must be hosted on Instagram or Facebook CDNs
    if 'cdninstagram.com' not in url_lower and 'fbcdn.net' not in url_lower:
        return False
        
    # Block static asset subdomains or paths
    if 'static.cdninstagram.com' in url_lower or '/rsrc.php/' in url_lower or 'data:image/' in url_lower:
        return False
        
    # Block common placeholder/fallback keywords
    placeholder_keywords = [
        'placeholder', 'default_profile', 'anonymoususer', 
        'avatar', 'silhouette', 'logo', 'icon', 'static_assets'
    ]
    for kw in placeholder_keywords:
        if kw in url_lower:
            return False
            
    # Block profile pictures (directory ends in -19, e.g., /t51.2885-19/ or /t51.82787-19/)
    if re.search(r'/t[^/]+-19/', url_lower):
        return False
        
    # Decode base64 parameters (like efg) and check for profile_pic keywords
    try:
        parsed_url = urllib.parse.urlparse(url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        for key, values in query_params.items():
            for value in values:
                try:
                    padded_val = value + '=' * (4 - len(value) % 4)
                    decoded_bytes = base64.b64decode(padded_val)
                    decoded_str = decoded_bytes.decode('utf-8', errors='ignore').lower()
                    if 'profile_pic' in decoded_str or 'profile_picture' in decoded_str:
                        return False
                except Exception:
                    pass
        # Check path for direct media extension
        path_lower = parsed_url.path.lower()
        if not any(ext in path_lower for ext in ['.mp4', '.jpg', '.jpeg', '.png']):
            return False
    except Exception:
        return False
        
    return True

def extract_shortcode(url):
    """
    Extracts the shortcode from an Instagram URL.
    """
    match = re.search(r'/(?:p|reel|tv|stories)/([A-Za-z0-9_-]+)', url)
    if match:
        return match.group(1)
    return None

def try_cobalt_instance(instance, url, payload, headers):
    endpoint = instance
    if "api.cobalt.tools" in instance:
        endpoint = "https://api.cobalt.tools"
    try:
        response = requests.post(endpoint, json=payload, headers=headers, timeout=6)
        if response.status_code == 200:
            data = response.json()
            status = data.get("status")
            if status in ["redirect", "stream", "tunnel"] or status == "picker":
                return {
                    "instance": instance,
                    "data": data
                }
    except Exception:
        pass
    return None

def try_cobalt(url):
    """
    Attempts to download media concurrently using all community Cobalt instances.
    Supports single media (redirect/stream) and carousels (picker).
    """
    payload = {
        "url": url,
        "videoQuality": "720"
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    endpoints = ["https://api.cobalt.tools"] + COBALT_INSTANCES
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(endpoints)) as executor:
        futures = [
            executor.submit(try_cobalt_instance, inst, url, payload, headers) 
            for inst in endpoints
        ]
        
        # Return the first successful Cobalt result immediately
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                instance = result["instance"]
                data = result["data"]
                status = data.get("status")
                logger.info(f"Successful concurrent Cobalt response from {instance}")
                
                if status in ["redirect", "stream", "tunnel"]:
                    media_url = data.get("url")
                    if media_url:
                        return {
                            "status": "success",
                            "url": media_url,
                            "thumbnail": media_url,
                            "title": "Instagram Media"
                        }
                        
                elif status == "picker":
                    picker_items = data.get("picker", [])
                    media_list = []
                    for item in picker_items:
                        item_url = item.get("url")
                        item_thumb = item.get("thumb") or item_url
                        if item_url:
                            media_list.append({
                                "url": item_url,
                                "thumbnail": item_thumb,
                                "type": item.get("type", "photo")
                            })
                            
                    if media_list:
                        first_item = media_list[0]
                        return {
                            "status": "success",
                            "url": first_item["url"],
                            "thumbnail": first_item["thumbnail"],
                            "title": "Instagram Carousel",
                            "media": media_list
                        }
                        
    return None

def extract_with_ytdlp(url):
    """
    Fallback extractor using yt-dlp to retrieve video and metadata.
    """
    logger.info(f"Attempting fallback extraction with yt-dlp: {url}")
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'best',
        'socket_timeout': 6,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            media_url = info.get('url')
            if media_url:
                thumbnail = info.get('thumbnail') or info.get('thumbnails', [{}])[0].get('url') or media_url
                title = info.get('title') or info.get('description') or "Instagram Media"
                if len(title) > 100:
                    title = title[:97] + "..."
                return {
                    "status": "success",
                    "url": media_url,
                    "thumbnail": thumbnail,
                    "title": title
                }
    except Exception as e:
        logger.error(f"yt-dlp extraction failed: {e}")
    return None

def scrape_instagram_embed(shortcode):
    """
    Scrapes the Instagram Embed Page and parses meta tags for single photos.
    """
    embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
    logger.info(f"Scraping Instagram Embed: {embed_url}")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/"
    }
    
    try:
        response = requests.get(embed_url, headers=headers, timeout=8, verify=False)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            image_url = None
            
            # 1. Extract from og:image meta tag
            og_image = soup.find('meta', property='og:image')
            if og_image and og_image.get('content'):
                image_url = og_image.get('content')
                
            # 2. Extract from twitter:image meta tag
            if not image_url:
                tw_image = soup.find('meta', attrs={'name': 'twitter:image'})
                if tw_image and tw_image.get('content'):
                    image_url = tw_image.get('content')
                    
            # 3. Extract from .EmbeddedMediaImage class tags
            if not image_url:
                img_tag = soup.find('img', class_='EmbeddedMediaImage') or soup.select_one('.EmbeddedMediaImage')
                if img_tag and img_tag.get('src'):
                    image_url = img_tag.get('src')
                    
            # Double fallback to any cdninstagram image in the page
            if not image_url:
                for img in soup.find_all('img'):
                    src = img.get('src')
                    if src and 'cdninstagram.com' in src and not src.startswith('data:'):
                        image_url = src
                        break
                        
            # Extract post caption as title
            title = "Instagram Photo"
            caption_div = soup.find('div', class_='Caption') or soup.find('div', class_='CaptionText')
            if caption_div:
                title = caption_div.get_text().strip()
                if len(title) > 100:
                    title = title[:97] + "..."
                    
            if image_url:
                return {
                    "status": "success",
                    "url": image_url,
                    "thumbnail": image_url,
                    "title": title,
                    "type": "photo"
                }
    except Exception as e:
        logger.error(f"Failed to scrape Instagram embed: {e}")
        
    return None

def scrape_instagram_story_direct(story_id):
    """
    Scrapes the Instagram direct story endpoints and parses meta tags.
    """
    urls = [
        f"https://www.instagram.com/stories/media/{story_id}/",
        f"https://www.instagram.com/p/{story_id}/embed/captioned/"
    ]
    
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/"
    }
    
    for url in urls:
        logger.info(f"Direct Story Scraper querying: {url}")
        try:
            response = requests.get(url, headers=headers, timeout=8, verify=False)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                
                extracted_url = None
                
                # 1. Try og:video (for video stories)
                og_video = soup.find('meta', property='og:video') or soup.find('meta', attrs={'name': 'og:video'})
                if og_video and og_video.get('content'):
                    extracted_url = og_video.get('content')
                    
                # 2. Try twitter:player (video fallback)
                if not extracted_url:
                    tw_player = soup.find('meta', attrs={'name': 'twitter:player'}) or soup.find('meta', property='twitter:player')
                    if tw_player and tw_player.get('content'):
                        extracted_url = tw_player.get('content')
                        
                # 3. Try og:image (for photo stories)
                if not extracted_url:
                    og_image = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})
                    if og_image and og_image.get('content'):
                        extracted_url = og_image.get('content')
                        
                if extracted_url:
                    extracted_url = extracted_url.replace('&amp;', '&').replace('\\u0026', '&')
                    if 'profile_pic' in extracted_url:
                        logger.info("Direct scraper matched profile picture. Skipping.")
                        continue
                        
                    logger.info(f"Direct Story Scraper SUCCESS! Extracted URL: {extracted_url[:120]}")
                    return {
                        "status": "success",
                        "url": extracted_url,
                        "thumbnail": extracted_url,
                        "title": "Instagram Story",
                        "type": "story"
                    }
        except Exception as e:
            logger.error(f"Direct Story Scraper failed for url {url}: {e}")
            
    return None

@app.route('/api/download', methods=['POST', 'GET'])
@app.route('/download', methods=['POST', 'GET'])
def download_media():
    """
    Main download route mapping requests to their tab handlers.
    """
    # Read JSON or fallback to args/form
    data = request.get_json(silent=True) or {}
    raw_url = data.get('url') or data.get('link') or request.args.get('url') or request.form.get('url')
    media_type = data.get('type') or request.args.get('type') or 'reel'
    
    # Map plural forms from frontend tabs (reels, photos, stories) to singular forms
    if media_type == 'reels':
        media_type = 'reel'
    elif media_type == 'photos':
        media_type = 'photo'
    elif media_type == 'stories':
        media_type = 'story'

    if not raw_url:
        return jsonify({"status": "error", "error": "Please enter a valid Instagram URL."}), 400
        
    if media_type not in ["reel", "photo", "story"]:
        return jsonify({
            "status": "error",
            "error": "Invalid media type. Must be 'reel', 'photo', or 'story'."
        }), 400
        
    # Clean URL before processing
    clean_url = sanitize_url(raw_url)
    logger.info(f"Processing Request - Type: {media_type}, Sanitized URL: {clean_url}")
    
    result = None
    
    # --- Handler: REELS ---
    if media_type == "reel":
        # 1. Try Cobalt API rotation
        result = try_cobalt(clean_url)
        # 2. Fallback to yt-dlp
        if not result:
            result = extract_with_ytdlp(clean_url)
            
    # --- Handler: PHOTOS ---
    elif media_type == "photo":
        shortcode = extract_shortcode(clean_url)
        # 1. Try Cobalt API rotation first (handles carousels / picker array)
        result = try_cobalt(clean_url)
        
        # Verify Cobalt did not return an empty result
        if result and result.get("status") == "success" and not result.get("url") and not result.get("media"):
            logger.info("Cobalt returned success but empty media/url. Invalidating Cobalt result.")
            result = None
            
        # 2. Fallback to Embed Page Scraping (reliable for single photos via Googlebot User-Agent)
        if not result and shortcode:
            result = scrape_instagram_embed(shortcode)
            
        # 3. Final fallback to yt-dlp
        if not result:
            result = extract_with_ytdlp(clean_url)
            
    # --- Handler: STORIES ---
    elif media_type == "story":
        # Extract username or story ID from stories URL pattern
        story_match = re.search(r'/stories/([^/?#]+)', clean_url)
        if story_match:
            target = story_match.group(1)
            logger.info(f"Processing Story Target: {target}")
            
        # 1. Try Cobalt API rotation
        result = try_cobalt(clean_url)
        
        # 2. Fallback to Direct Story Scraper (if story ID is available)
        if not result:
            story_id_match = re.search(r'/stories/[^/]+/([0-9A-Za-z_-]+)', clean_url)
            if story_id_match:
                story_id = story_id_match.group(1)
                logger.info(f"Cobalt failed. Falling back to Direct Story Scraper for ID: {story_id}")
                result = scrape_instagram_story_direct(story_id)
            
    # --- Validate Results ---
    if result and result.get("status") == "success":
        main_url = result.get("url")
        if main_url and not is_valid_instagram_cdn_media(main_url):
            logger.info(f"Invalid main URL filtered: {main_url}")
            result["url"] = None
            
        thumb_url = result.get("thumbnail")
        if thumb_url and not is_valid_instagram_cdn_media(thumb_url):
            logger.info(f"Invalid thumbnail URL filtered: {thumb_url}")
            result["thumbnail"] = None
            
        if "media" in result and isinstance(result["media"], list):
            valid_media = []
            for item in result["media"]:
                item_url = item.get("url")
                if item_url and is_valid_instagram_cdn_media(item_url):
                    item_thumb = item.get("thumbnail")
                    if item_thumb and not is_valid_instagram_cdn_media(item_thumb):
                        item["thumbnail"] = item_url
                    valid_media.append(item)
                else:
                    logger.info(f"Invalid carousel item filtered: {item_url}")
            if valid_media:
                result["media"] = valid_media
                if not result.get("url"):
                    result["url"] = valid_media[0]["url"]
                if not result.get("thumbnail"):
                    result["thumbnail"] = valid_media[0].get("thumbnail") or result["url"]
            else:
                result["media"] = None
                result["url"] = None
                result["thumbnail"] = None
                
    # --- Return Response ---
    if result and result.get("status") == "success" and result.get("url"):
        # Ensure all required fields exist
        response_data = {
            "status": "success",
            "url": result.get("url"),
            "thumbnail": result.get("thumbnail") or result.get("url"),
            "title": result.get("title", "Instagram Content"),
            "type": result.get("type") or media_type
        }
        # Include carousel/multiple media list if present
        if "media" in result and result["media"]:
            response_data["media"] = result["media"]
        return jsonify(response_data), 200
    else:
        logger.error(f"Download handler failed to extract media from URL: {clean_url}")
        if media_type == "story":
            return jsonify({
                "status": "error",
                "error": "Real story video/photo URL could not be extracted. Please try direct story link or verify account has public active stories."
            }), 400
            
        return jsonify({
            "status": "error",
            "error": "Public media fetch nahi ho saka. Link recheck karein!"
        }), 400

@app.route('/api/proxy_download')
def proxy_download():
    media_url = request.args.get('url')
    filename = request.args.get('filename', 'instafetch_media.mp4')
    if not media_url:
        return "URL missing", 400

    r = requests.get(media_url, stream=True)
    return Response(
        r.iter_content(chunk_size=1024*1024),
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Type': r.headers.get('Content-Type', 'video/mp4')
        }
    )

if __name__ == '__main__':
    app.run(debug=True, port=5000)
