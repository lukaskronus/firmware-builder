import requests
from bs4 import BeautifulSoup
import time
import os
import sys
import json

BASE_URL = "http://ec2-3-39-192-44.ap-northeast-2.compute.amazonaws.com"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) GitHub-Actions-Crawler',
    'X-Requested-With': 'fetch'
}

def get_max_pages(session):
    """Automatically detects the total number of pages from the main card grid."""
    try:
        url = f"{BASE_URL}/en/card/"
        response = session.get(url, timeout=30)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # The site stores the total page count in a data attribute: <div class="card-search" data-page-total="203">
        search_root = soup.find(attrs={"data-card-search": True})
        if search_root and search_root.has_attr('data-page-total'):
            total_pages = int(search_root['data-page-total'])
            print(f"Auto-detected maximum pages: {total_pages}")
            return total_pages
        return 1
    except Exception as e:
        print(f"Failed to detect max pages: {e}")
        return 1

def get_card_ids(session, max_pages, delay):
    card_ids = []
    for page in range(1, max_pages + 1):
        print(f"Scanning grid page {page}/{max_pages}...")
        try:
            url = f"{BASE_URL}/en/card/?page={page}"
            response = session.get(url, timeout=30)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            cards = soup.find_all(attrs={"data-card-id": True})
            for card in cards:
                card_ids.append(card['data-card-id'])
        except Exception as e:
            print(f"Error on page {page}: {e}")
            
        time.sleep(delay)
        
    return list(set(card_ids))

def scrape_card_data(card_id, session):
    detail_url = f"{BASE_URL}/en/card/select/{card_id}"
    res = session.get(detail_url, timeout=30)
    data = res.json()
    
    detail_soup = BeautifulSoup(data['html'], 'html.parser')
    
    hp_stat = detail_soup.find(class_='card-tile__stat--hp')
    hp = hp_stat.text.strip() if hp_stat else "Unknown"
    
    title_elem = detail_soup.find(class_='card-detail__title')
    name = title_elem.text.strip() if title_elem else f"Card_{card_id}"
    
    img_url = f"{BASE_URL}/static/card/Texture2D/CARD_{card_id}.webp"
    img_data = session.get(img_url, timeout=30).content
    
    return {
        "id": card_id,
        "name": name,
        "hp": hp,
        "image_url": img_url
    }, img_data

if __name__ == "__main__":
    delay = float(sys.argv[1]) if len(sys.argv) > 1 else 0.5
    
    os.makedirs("kana_db_data", exist_ok=True)
    os.makedirs("kana_db_images", exist_ok=True)
    
    session = requests.Session()
    session.headers.update(HEADERS)
    
    # Automatically detect the maximum number of pages
    max_pages = get_max_pages(session)
    
    print(f"Fetching card IDs from {max_pages} pages...")
    ids = get_card_ids(session, max_pages, delay)
    print(f"Found {len(ids)} unique cards.")
    
    success_count = 0
    for cid in ids:
        try:
            info, img_bytes = scrape_card_data(cid, session)
            
            # Save JSON
            with open(f"kana_db_data/{info['id']}.json", "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=4)
                
            # Save Image
            with open(f"kana_db_images/{info['id']}.webp", "wb") as f:
                f.write(img_bytes)
                
            success_count += 1
            print(f"[{success_count}/{len(ids)}] Scraped {info['name']} ({cid})")
            
        except Exception as e:
            print(f"Failed to scrape {cid}: {e}")
            
        time.sleep(delay)

    print(f"Crawl complete. Successfully scraped {success_count} cards.")
