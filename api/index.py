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

def find_video_url_recursive(obj):
    if isinstance(obj, str):
        # Match direct video stream links (containing mp4 or video indicators)
        if obj.startswith("http") and (".mp4" in obj.lower() or "mp4" in obj.lower() or "/video/" in obj.lower()):
            return obj
        return None
    elif isinstance(obj, dict):
        # Prioritize key names commonly containing direct video URLs
        video_keys = ["video_url", "video_versions", "download_url", "downloadLink", "high_quality_url", "url", "link"]
        for key in video_keys:
            val = obj.get(key)
            if val:
                # Resolve list arrays like video_versions
                if key == "video_versions" and isinstance(val, list):
                    for item in val:
                        if isinstance(item, dict):
                            url_val = item.get("url")
                            if url_val and isinstance(url_val, str) and url_val.startswith("http"):
                                return url_val
                found = find_video_url_recursive(val)
                if found:
                    return found
        # Recursively search other keys
        for k, v in obj.items():
            if k not in video_keys:
                found = find_video_url_recursive(v)
                if found:
                    return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_video_url_recursive(item)
            if found:
                return found
    return None

def check_and_update_video_payload(data, is_reel_request=False):
    if not isinstance(data, dict):
        return data
    
    is_video = False
    
    # 1. Update media list items first if present
    if "media" in data and isinstance(data["media"], list):
        for item in data["media"]:
            item_video = find_video_url_recursive(item)
            if item_video:
                item["url"] = item_video
                item["type"] = "video"
                is_video = True
                
    # 2. Check root level
    root_video = find_video_url_recursive(data)
    if root_video:
        data["url"] = root_video
        is_video = True
    else:
        # Fallback check on string representations or type flags
        main_url = data.get("url", "") or ""
        if data.get("type") == "video" or data.get("is_video") or ".mp4" in main_url.lower() or "mp4" in main_url.lower():
            is_video = True
            
    if is_video and data.get("type") != "story":
        data["type"] = "video"
        
    # Strictly enforce for Reels/Videos: if requested as reel/video, we must have a video URL.
    # Do NOT fallback to a display_url or thumbnail_url (which are images).
    if is_reel_request:
        current_url = data.get("url", "") or ""
        if not (current_url.startswith("http") and (".mp4" in current_url.lower() or "mp4" in current_url.lower() or "/video/" in current_url.lower() or "d.rapidcdn.app" in current_url.lower())):
            logger.warning(f"Reel request resolved to non-video URL: {current_url}. Invalidating.")
            data["url"] = None
            data["status"] = "error"
            data["error"] = "Could not extract video stream for this Reel."
            
    return data

import re

def extract_mp4_links_from_text(text):
    # Regex to find absolute URL patterns
    urls = re.findall(r'https?://[^\s"\'\\<>]+', text)
    video_urls = []
    for url in urls:
        url_clean = url.replace('&amp;', '&').replace('\\/', '/')
        # Strip trailing quote/backslash characters often found in JSON strings
        url_clean = url_clean.rstrip('\\"\'')
        if ".mp4" in url_clean.lower() or "mp4" in url_clean.lower() or "/video/" in url_clean.lower():
            video_urls.append(url_clean)
    return list(set(video_urls))

def parse_proxy_url(clean_url, domain="vxinstagram.com"):
    # Convert instagram.com to proxy domain safely
    if "www.instagram.com" in clean_url:
        url = clean_url.replace("www.instagram.com", domain)
    else:
        url = clean_url.replace("instagram.com", domain)
        
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Twitterbot/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        logger.info(f"Querying {domain} fallback URL: {url}")
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            logger.warning(f"{domain} returned status {response.status_code} for URL {url}")
            return None
            
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Extract title from og:description or page title
        title = "Instagram Story" if "/stories/" in clean_url.lower() else "Instagram Media"
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
                
        # Scan raw response text for any nested/hidden direct video stream (.mp4) links
        extracted_videos = extract_mp4_links_from_text(response.text)
        for video_url in extracted_videos:
            if not any(x["url"] == video_url for x in media_list):
                media_list.append({
                    "url": video_url,
                    "thumbnail": video_url,
                    "type": "video"
                })
                
        if not media_list:
            logger.warning(f"No media parsed from {domain} fallback for {url}")
            return None
            
        # Structure the final JSON cleanly for the frontend
        response_type = "story" if "/stories/" in clean_url.lower() else ("photo" if all(x["type"] == "photo" for x in media_list) else "video")
        
        if len(media_list) > 1:
            return {
                "status": "success",
                "is_proxy": True,
                "url": media_list[0]["url"],
                "thumbnail": media_list[0]["thumbnail"],
                "title": title,
                "media": media_list,
                "type": response_type
            }
        else:
            first = media_list[0]
            return {
                "status": "success",
                "is_proxy": True,
                "url": first["url"],
                "thumbnail": first["thumbnail"],
                "title": title,
                "type": "story" if response_type == "story" else first["type"]
            }
            
    except Exception as e:
        logger.error(f"Error parsing {domain} fallback: {e}")
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
            status = data.get("status")
            if status in ["redirect", "stream", "tunnel"]:
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
                    return {
                        "status": "success",
                        "is_cobalt": True,
                        "url": media_list[0]["url"],
                        "thumbnail": media_list[0]["thumbnail"],
                        "title": "Instagram Media",
                        "media": media_list,
                        "type": "photo" if all(x["type"] == "photo" for x in media_list) else "video"
                    }
    except Exception as e:
        logger.debug(f"Cobalt fallback failed: {e}")
    return None

def wrapped_download_media():
    """
    Guaranteed public API/proxy fallback layer wrapper.
    Executes the original logic and falls back on error or rate-limiting block.
    """
    # Extract raw URL and type from request parameters
    req_data = request.get_json(silent=True) or {}
    raw_url = req_data.get('url') or req_data.get('link') or request.args.get('url') or request.form.get('url')
    req_type = req_data.get('type') or request.args.get('type') or request.form.get('type') or 'reel'
    
    clean_url = sanitize_url(raw_url) if raw_url else ""
    url_lower = clean_url.lower()
    
    is_reel_request = (req_type in ['reel', 'reels']) or ('/reel/' in url_lower or '/tv/' in url_lower)
    
    # Store the best available image/photo fallback result
    best_image_fallback = None
    
    if original_download_media:
        try:
            res = original_download_media()
            if isinstance(res, tuple):
                response_data, status_code = res
                if status_code == 200:
                    data = response_data.get_json() if hasattr(response_data, 'get_json') else response_data
                    if data:
                        updated_data = check_and_update_video_payload(data, is_reel_request)
                        if updated_data.get("status") == "success" and updated_data.get("url"):
                            return jsonify(updated_data), 200
                        elif updated_data.get("url"):
                            best_image_fallback = updated_data
            else:
                return res
        except Exception as e:
            logger.error(f"Original download_media function failed with exception: {e}")
            
    logger.info("Instagram rate-limiting detected. Running fallback extraction layer...")
    
    if not raw_url:
        return jsonify({"status": "error", "error": "Please enter a valid Instagram URL."}), 400
        
    # 1. Fallback: vxinstagram.com Metadata Scraping Proxy
    result = parse_proxy_url(clean_url, "vxinstagram.com")
    if result:
        logger.info("Fallback extraction succeeded using vxinstagram!")
        updated_result = check_and_update_video_payload(result, is_reel_request)
        if updated_result.get("status") == "success" and updated_result.get("url"):
            return jsonify(updated_result), 200
        elif updated_result.get("url") and not best_image_fallback:
            best_image_fallback = updated_result
            
    # 2. Fallback: ddinstagram.com Metadata Scraping Proxy
    result = parse_proxy_url(clean_url, "ddinstagram.com")
    if result:
        logger.info("Fallback extraction succeeded using ddinstagram!")
        updated_result = check_and_update_video_payload(result, is_reel_request)
        if updated_result.get("status") == "success" and updated_result.get("url"):
            return jsonify(updated_result), 200
        elif updated_result.get("url") and not best_image_fallback:
            best_image_fallback = updated_result
            
    # 3. Fallback: Instagram oEmbed API Page Parsing (Only applies to posts/reels)
    result = fetch_oembed_fallback(clean_url)
    if result:
        logger.info("Fallback extraction succeeded using oEmbed!")
        updated_result = check_and_update_video_payload(result, is_reel_request)
        if updated_result.get("status") == "success" and updated_result.get("url"):
            return jsonify(updated_result), 200
        elif updated_result.get("url") and not best_image_fallback:
            best_image_fallback = updated_result
        
    # 4. Fallback: api.cobalt.tools (Supports stories, reels, posts)
    result = try_cobalt_fallback(clean_url)
    if result:
        logger.info("Fallback extraction succeeded using Cobalt API!")
        updated_result = check_and_update_video_payload(result, is_reel_request)
        if updated_result.get("status") == "success" and updated_result.get("url"):
            return jsonify(updated_result), 200
        elif updated_result.get("url") and not best_image_fallback:
            best_image_fallback = updated_result
            
    # Safe fallback: if we failed to get a direct playable video stream link, but have an image/thumbnail fallback,
    # return it properly formatted for the frontend player.
    if best_image_fallback:
        logger.info("Direct video stream extraction failed. Returning best available image/photo fallback payload.")
        best_image_fallback["status"] = "success"
        if "error" in best_image_fallback:
            del best_image_fallback["error"]
        return jsonify(best_image_fallback), 200
        
    # All extraction attempts failed
    logger.error(f"All fallback layers failed to retrieve media for URL: {clean_url}")
    return jsonify({
        "status": "error",
        "error": "Public media fetch nahi ho saka. Link recheck karein!"
    }), 400

# Overwrite route mapping on the app
if 'download_media' in app.view_functions:
    app.view_functions['download_media'] = wrapped_download_media
