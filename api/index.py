import sys
import os
import logging
import requests
import urllib.parse
from bs4 import BeautifulSoup
from flask import request, jsonify

# Add root folder to python path to resolve imports from app.py
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, sanitize_url, extract_shortcode

logger = logging.getLogger(__name__)

# Save the original download_media view function to wrap it
original_download_media = app.view_functions.get('download_media')

def check_and_update_video_payload(data):
    if not isinstance(data, dict):
        return data
    
    is_video = False
    
    # Check at root level
    main_url = data.get("url", "") or ""
    if data.get("type") == "video" or data.get("type") == "story" or data.get("is_video") or ".mp4" in main_url.lower() or "mp4" in main_url.lower():
        is_video = True
        
    # Check media list if present
    if "media" in data and isinstance(data["media"], list):
        for item in data["media"]:
            item_url = item.get("url", "") or ""
            if item.get("type") == "video" or item.get("type") == "story" or ".mp4" in item_url.lower() or "mp4" in item_url.lower():
                item["type"] = "video"
                is_video = True
                
    if is_video:
        data["type"] = "video"
        
    return data

def parse_vxinstagram_fallback(shortcode):
    url = f"https://vxinstagram.com/p/{shortcode}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Twitterbot/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        logger.info(f"Querying vxinstagram fallback for shortcode: {shortcode}")
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            logger.warning(f"vxinstagram returned status {response.status_code} for shortcode {shortcode}")
            return None
            
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Extract title from og:description or page title
        title = "Instagram Media"
        og_desc = soup.find('meta', property='og:description') or soup.find('meta', attrs={'name': 'og:description'})
        if og_desc and og_desc.get('content'):
            title = og_desc.get('content').strip()
            if len(title) > 100:
                title = title[:97] + "..."
                
        # Parse multiple slides (cards) for carousel support
        media_list = []
        cards = soup.select('div.card')
        for card in cards:
            dl_btn = card.find('a', class_='btn-success')
            if not dl_btn or not dl_btn.get('href'):
                continue
            dl_url = dl_btn.get('href')
            
            # Identify video vs photo
            is_video = card.find('video') is not None or ".mp4" in dl_url.lower() or "mp4" in dl_url.lower()
            img_tag = card.find('img')
            thumb_url = img_tag.get('src') if img_tag else dl_url
            
            media_list.append({
                "url": dl_url,
                "thumbnail": thumb_url,
                "type": "video" if is_video else "photo"
            })
            
        # Fallback to standard meta tags for single items
        if not media_list:
            video_url = None
            image_url = None
            
            og_video = soup.find('meta', property='og:video') or soup.find('meta', attrs={'name': 'og:video'})
            if og_video and og_video.get('content'):
                video_url = og_video.get('content')
                
            og_image = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'og:image'})
            if og_image and og_image.get('content'):
                image_url = og_image.get('content')
                
            if video_url or (image_url and (".mp4" in image_url.lower() or "mp4" in image_url.lower())):
                media_list.append({
                    "url": video_url or image_url,
                    "thumbnail": image_url,
                    "type": "video"
                })
            elif image_url:
                media_list.append({
                    "url": image_url,
                    "thumbnail": image_url,
                    "type": "photo"
                })
                
        if not media_list:
            logger.warning(f"No media parsed from vxinstagram fallback for {shortcode}")
            return None
            
        # Structure the final JSON cleanly for the frontend
        if len(media_list) > 1:
            return {
                "status": "success",
                "is_proxy": True,
                "url": media_list[0]["url"],
                "thumbnail": media_list[0]["thumbnail"],
                "title": title,
                "media": media_list,
                "type": "photo" if all(x["type"] == "photo" for x in media_list) else "video"
            }
        else:
            first = media_list[0]
            return {
                "status": "success",
                "is_proxy": True,
                "url": first["url"],
                "thumbnail": first["thumbnail"],
                "title": title,
                "type": first["type"]
            }
            
    except Exception as e:
        logger.error(f"Error parsing vxinstagram fallback: {e}")
        return None

def fetch_oembed_fallback(url):
    oembed_url = f"https://api.instagram.com/oembed/?url={urllib.parse.quote(url)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        logger.info("Querying oEmbed fallback...")
        response = requests.get(oembed_url, headers=headers, timeout=5)
        if response.status_code == 200:
            try:
                # Try parsing JSON
                data = response.json()
                image_url = data.get("thumbnail_url")
                title = data.get("title") or "Instagram Content"
                if len(title) > 100:
                    title = title[:97] + "..."
                if image_url:
                    return {
                        "status": "success",
                        "is_oembed": True,
                        "url": image_url,
                        "thumbnail": image_url,
                        "title": title,
                        "type": "photo"
                    }
            except Exception:
                # Fallback to parsing HTML response directly if redirected to consent page
                soup = BeautifulSoup(response.text, 'html.parser')
                image_url = None
                og_image = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'og:image'})
                if og_image and og_image.get('content'):
                    image_url = og_image.get('content')
                
                title = "Instagram Content"
                if soup.title:
                    title = soup.title.text
                    
                if image_url:
                    return {
                        "status": "success",
                        "is_oembed": True,
                        "url": image_url,
                        "thumbnail": image_url,
                        "title": title,
                        "type": "photo"
                    }
    except Exception as e:
        logger.error(f"Error in oEmbed fallback: {e}")
    return None

def try_cobalt_fallback(url):
    payload = {
        "url": url,
        "videoQuality": "720"
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        logger.info("Querying cobalt tools fallback...")
        response = requests.post("https://api.cobalt.tools", json=payload, headers=headers, timeout=6)
        if response.status_code == 200:
            data = response.json()
            if data.get("status") in ["redirect", "stream", "tunnel"]:
                media_url = data.get("url")
                if media_url:
                    return {
                        "status": "success",
                        "is_cobalt": True,
                        "url": media_url,
                        "thumbnail": media_url,
                        "title": "Instagram Media",
                        "type": "video"
                    }
    except Exception as e:
        logger.debug(f"Cobalt fallback failed: {e}")
    return None

def wrapped_download_media():
    """
    Guaranteed public API/proxy fallback layer wrapper.
    Executes the original logic and falls back on error or rate-limiting block.
    """
    if original_download_media:
        try:
            res = original_download_media()
            if isinstance(res, tuple):
                response_data, status_code = res
                if status_code == 200:
                    data = response_data.get_json() if hasattr(response_data, 'get_json') else response_data
                    if data:
                        updated_data = check_and_update_video_payload(data)
                        return jsonify(updated_data), 200
                    return res
            else:
                return res
        except Exception as e:
            logger.error(f"Original download_media function failed with exception: {e}")
            
    logger.info("Instagram rate-limiting detected. Running fallback extraction layer...")
    
    # Extract raw URL from request parameters
    data = request.get_json(silent=True) or {}
    raw_url = data.get('url') or data.get('link') or request.args.get('url') or request.form.get('url')
    if not raw_url:
        return jsonify({"status": "error", "error": "Please enter a valid Instagram URL."}), 400
        
    clean_url = sanitize_url(raw_url)
    shortcode = extract_shortcode(clean_url)
    
    # 1. Fallback: vxinstagram.com Metadata Scraping Proxy
    if shortcode:
        result = parse_vxinstagram_fallback(shortcode)
        if result:
            logger.info("Fallback extraction succeeded using vxinstagram!")
            updated_result = check_and_update_video_payload(result)
            return jsonify(updated_result), 200
            
    # 2. Fallback: Instagram oEmbed API Page Parsing
    result = fetch_oembed_fallback(clean_url)
    if result:
        logger.info("Fallback extraction succeeded using oEmbed!")
        updated_result = check_and_update_video_payload(result)
        return jsonify(updated_result), 200
        
    # 3. Fallback: api.cobalt.tools
    result = try_cobalt_fallback(clean_url)
    if result:
        logger.info("Fallback extraction succeeded using Cobalt API!")
        updated_result = check_and_update_video_payload(result)
        return jsonify(updated_result), 200
        
    # All extraction attempts failed
    logger.error(f"All fallback layers failed to retrieve media for URL: {clean_url}")
    return jsonify({
        "status": "error",
        "error": "Public media fetch nahi ho saka. Link recheck karein!"
    }), 400

# Overwrite route mapping on the app
if 'download_media' in app.view_functions:
    app.view_functions['download_media'] = wrapped_download_media
