import asyncio
import aiohttp
from bs4 import BeautifulSoup
import json
import os
import re
import argparse
import sys

# Configuration
BASE_URL = "http://ec2-3-39-192-44.ap-northeast-2.compute.amazonaws.com"
DELAY = 0.3
INCLUDE_UNCOLLECTABLE = True
OUTPUT_FILE = "kana_cards_final.json"

CATEGORY_MAP = {"1": "Character", "2": "Spell", "3": "Follower"}

async def get_card_ids(session):
    """Fast discovery of all card IDs."""
    ids = set()
    params = "search_type=name&search_text=&etc=uncollectable&tab=etc&deck_tab=edit" if INCLUDE_UNCOLLECTABLE else "tab=detail"
    url = f"{BASE_URL}/en/card/?{params}&page=1"
    
    async with session.get(url) as response:
        html = await response.text()
        soup = BeautifulSoup(html, 'html.parser')
        root = soup.find(attrs={"data-card-search": True})
        max_pages = int(root["data-page-total"]) if root and root.has_attr("data-page-total") else 1

    print(f"Found {max_pages} pages. Discovering IDs...")
    tasks = []
    for page in range(1, max_pages + 1):
        page_url = f"{BASE_URL}/en/card/?{params}&page={page}"
        tasks.append(fetch_page_ids(session, page_url))
    
    results = await asyncio.gather(*tasks)
    for page_ids in results:
        ids.update(page_ids)
    return sorted(ids)

async def fetch_page_ids(session, url):
    async with session.get(url) as response:
        html = await response.text()
        soup = BeautifulSoup(html, 'html.parser')
        return [card["data-card-id"] for card in soup.find_all(attrs={"data-card-id": True})]

async def fetch_enhancement_chain(session, start_id):
    """Fetch full enhancement chain sequentially."""
    chain = []
    current_id = start_id
    visited = set()
    while current_id != -1 and current_id not in visited:
        visited.add(current_id)
        url = f"{BASE_URL}/en/card/select/{current_id}"
        try:
            async with session.get(url) as response:
                data = await response.json()
                state = data.get("state", {})
                html = data.get("html", "")
                if not html: break
                soup = BeautifulSoup(html, "html.parser")
                enh_el = soup.select_one(".card-tile__stat--enhance")
                hp_el = soup.select_one(".card-tile__stat--hp")
                chain.append({
                    "card_id": current_id,
                    "level": int(enh_el.get_text(strip=True)) if enh_el else 0,
                    "hp": int(hp_el.get_text(strip=True)) if hp_el else 0,
                    "has_product": state.get("has_product", False)
                })
                current_id = state.get("enh_next", -1)
                await asyncio.sleep(DELAY)
        except Exception:
            break
    return chain

async def scrape_card_batch(session, batch_ids):
    """Fetch base details for a batch of cards concurrently."""
    tasks = []
    for cid in batch_ids:
        url = f"{BASE_URL}/en/card/select/{cid}"
        tasks.append(session.get(url))
    
    responses = await asyncio.gather(*tasks, return_exceptions=True)
    cards_data = []
    
    for cid, response in zip(batch_ids, responses):
        if isinstance(response, Exception):
            continue
        try:
            data = await response.json()
            if "html" not in data: continue
            
            soup = BeautifulSoup(data["html"], "html.parser")
            state = data.get("state", {})
            
            # Basic Info
            name_el = soup.select_one(".card-detail__name")
            name = name_el.get_text(strip=True) if name_el else f"Card_{cid}"
            
            # Materials
            products_el = soup.select_one('[data-card-products]')
            materials = []
            if products_el:
                for row in products_el.select('tr'):
                    cells = row.find_all('td')
                    if len(cells) >= 2:
                        img_match = re.search(r'url\(([^)]+)\)', cells[0].get('style', ''))
                        materials.append({
                            "image": img_match.group(1) if img_match else "",
                            "name": cells[1].get_text(strip=True)
                        })

            # Enhancement Chain (Only for base cards)
            enh_chain = []
            if state.get("enh_prev", -1) == -1:
                enh_chain = await fetch_enhancement_chain(session, cid)
                
            cards_data.append({
                "id": cid,
                "name": name,
                "category": CATEGORY_MAP.get(cid[0], "Unknown"),
                "producible": len(materials) > 0,
                "materials": materials,
                "enhancement_chain": enh_chain,
                "stats": {
                    "hp": soup.select_one(".card-tile__stat--hp").get_text(strip=True) if soup.select_one(".card-tile__stat--hp") else "0"
                }
            })
        except Exception:
            continue
            
    return cards_data

async def main():
    print("Starting optimized async scraper...")
    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        
        # 1. Discovery
        card_ids = await get_card_ids(session)
        print(f"Total cards to process: {len(card_ids)}")
        
        # 2. Process in Batches
        all_cards = []
        batch_size = 10 # Fetch 10 base cards at once
        
        for i in range(0, len(card_ids), batch_size):
            batch = card_ids[i:i+batch_size]
            print(f"Processing batch {i//batch_size + 1} ({len(batch)} cards)...")
            
            cards = await scrape_card_batch(session, batch)
            all_cards.extend(cards)
            
            # Save progress every 100 cards
            if len(all_cards) % 100 == 0:
                with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
                    json.dump(all_cards, f, ensure_ascii=False, indent=2)
                print(f"  💾 Saved progress: {len(all_cards)} cards.")
                
            await asyncio.sleep(DELAY * batch_size) # Delay after each batch
            
        # Final Save
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(all_cards, f, ensure_ascii=False, indent=2)
        print(f"✅ Finished! Saved {len(all_cards)} cards to {OUTPUT_FILE}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", type=str, default="official", choices=["official", "uncollectable"])
    parser.add_argument("--delay", type=float, default=0.3)
    parser.add_argument("--output", type=str, default="all_cards.json")
    
    args = parser.parse_args()
    
    # Set global variables based on args
    DELAY = args.delay
    INCLUDE_UNCOLLECTABLE = (args.mode == "uncollectable")
    
    # Update output filename based on mode
    if args.mode == "official":
        OUTPUT_FILE = "all_cards_official.json"
    else:
        OUTPUT_FILE = "all_cards_uncollectable.json"
        
    asyncio.run(main())
