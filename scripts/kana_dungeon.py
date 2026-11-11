import asyncio
import aiohttp
from bs4 import BeautifulSoup
import json
import re

async def main():
    base_url = "http://ec2-3-39-192-44.ap-northeast-2.compute.amazonaws.com"
    catalog_url = f"{base_url}/en/npc/"
    
    async with aiohttp.ClientSession() as session:
        print("Fetching catalog page...")
        async with session.get(catalog_url) as response:
            html = await response.text()
            
        soup = BeautifulSoup(html, 'html.parser')
        table = soup.find('table', class_='npc-catalog')
        rows = table.find_all('tr')

        floors = []
        current_dungeons = []
        current_difficulty = ""
        # Matches standard floors AND special floor identifiers
        floor_regex = re.compile(
            r'^(\d+F|[A-Z]-Lab|Beach \w+|Transcendental Observer|'
            r'Beginner Observer|Loner Observer|Collector Observer|'
            r'Duelist Observer|Manager Observer)$'
        )
        episode_regex = re.compile(r'^(EP\d+|EV\d+)\s+(.*)$')

        for i, tr in enumerate(rows):
            th_section = tr.find('th', class_='npc-catalog__section')
            if th_section:
                next_tr = rows[i + 1]
                ths = next_tr.find_all('th')
                if ths:
                    current_dungeons = [th.text.strip().replace('\n', ' ') for th in ths]
                continue

            td_difficulty = tr.find('td', class_='npc-catalog__difficulty')
            if td_difficulty:
                current_difficulty = td_difficulty.text.strip()
                continue

            tds = tr.find_all('td')
            if tds and not td_difficulty:
                for j, td in enumerate(tds):
                    links = td.find_all('a')
                    if links:
                        for a in links:
                            link_text = a.text.strip().replace('\n', ' ')
                            
                            is_floor = bool(floor_regex.match(link_text))
                            
                            if is_floor:
                                raw_name = current_dungeons[j] if j < len(current_dungeons) else "Unknown"
                                floor_name = link_text
                            else:
                                raw_name = link_text
                                floor_name = "1F"

                            episode_match = episode_regex.match(raw_name)
                            if episode_match:
                                episode = episode_match.group(1)
                                dungeon = episode_match.group(2)
                            else:
                                episode = ""
                                dungeon = raw_name

                            floors.append({
                                "episode": episode,
                                "dungeon": dungeon,
                                "level": current_difficulty,
                                "floor": floor_name,
                                "link": a.get('href')
                            })
        
        print(f"Found {len(floors)} floors. Starting safe sequential fetch...")

        async def fetch_floor_cards(session, floor_info):
            full_url = f"{base_url}{floor_info['link']}"
            try:
                async with session.get(full_url) as response:
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    
                    # 1. Extract regular deck cards (with counts)
                    cards_data = {}
                    deck_container = soup.find('div', class_='deck-summary__cards')
                    if deck_container:
                        regular_cards = deck_container.find_all('div', class_='card-tile--compact')
                        for card in regular_cards:
                            card_id = card.get('data-card-id')
                            count_el = card.find('div', class_='card-tile__count-value')
                            if card_id and count_el:
                                cards_data[card_id] = int(count_el.text.strip())
                    
                    # 2. Extract boss/character card separately
                    character_card = None
                    boss_el = soup.find('div', class_='card-tile--character')
                    if boss_el:
                        character_card = boss_el.get('data-card-id')
                            
                    return {
                        **floor_info, 
                        "character_card": character_card,
                        "cards": cards_data
                    }
            except Exception as e:
                return {
                    **floor_info, 
                    "character_card": None, 
                    "cards": {}, 
                    "error": str(e)
                }

        results = []
        for i, floor in enumerate(floors):
            res = await fetch_floor_cards(session, floor)
            results.append(res)
            
            if (i + 1) % 50 == 0:
                print(f"Processed {i + 1} / {len(floors)} floors...")
                
            await asyncio.sleep(0.5)

        with open('npc_dungeons.json', 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
            print(f"Successfully saved {len(results)} records to npc_dungeons.json")

if __name__ == "__main__":
    asyncio.run(main())
