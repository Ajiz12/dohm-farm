#!/usr/bin/env python3
"""Claim all sDOHM unstakes via Chromium (Playwright).
Camoufox doesn't hydrate React on the portfolio page, so Chromium is required.
"""
from playwright.sync_api import sync_playwright
import os
import time
import re

SEED = os.environ.get('SEED_PHRASE', '')
PW   = os.environ.get('WALLET_PASSWORD', '')
if not SEED or not PW:
    print("[FATAL] SEED_PHRASE & WALLET_PASSWORD wajib di-set.")
    exit(1)

TOTAL = 0


def do_claim():
    global TOTAL
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page()

        # Connect / restore wallet
        pg.goto('https://testnet.dohm.finance/app/stake', wait_until='load', timeout=30000)
        time.sleep(5)
        c = pg.locator('button:has-text("Connect wallet"):visible')
        if c.count() > 0:
            c.first.click()
            time.sleep(2)
            r = pg.locator('button:has-text("Restore from recovery phrase"):visible')
            if r.count() > 0:
                r.first.click()
                time.sleep(2)
                pg.locator('textarea').fill(SEED)
                time.sleep(0.5)
                pg.locator('input[type="password"]').fill(PW)
                time.sleep(0.5)
                pg.locator('button:has-text("Restore"):visible').first.click()
                time.sleep(10)
                pg.goto('https://testnet.dohm.finance/app/stake', wait_until='load', timeout=30000)

        # Wait for SPA render
        for i in range(20):
            time.sleep(3)
            if pg.locator('button[aria-selected]').count() > 0:
                break

        # Navigate to Unstake tab → Claim link → Portfolio
        pg.locator('button:has-text("Unstake"):visible').first.click()
        time.sleep(5)

        cl = pg.locator('text=/Claim.*→/').first
        if cl.count() == 0:
            b.close()
            return 0, 0
        cl.click()
        time.sleep(20)

        # Count remaining unstakes
        body = pg.inner_text('body')
        m = re.search(r'(\d+)\s*unstakes ready', body)
        remaining = int(m.group(1)) if m else 0

        # Click Claim buttons (5 per batch, reload between batches)
        claimed = 0
        for rnd in range(20):
            cnt = pg.evaluate(
                '()=>{const b=Array.from(document.querySelectorAll("button"));'
                'return b.filter(x=>x.innerText.trim()==="Claim").length}'
            )
            if cnt == 0:
                break
            for i in range(min(cnt, 5)):
                pos = pg.evaluate(
                    '()=>{const b=Array.from(document.querySelectorAll("button"));'
                    'const c=b.find(x=>x.innerText.trim()==="Claim");'
                    'if(!c)return null;'
                    'c.scrollIntoView({block:"center"});'
                    'const r=c.getBoundingClientRect();'
                    'return{x:r.x+r.width/2,y:r.y+r.height/2}}'
                )
                if pos and pos.get('y', 0) > 0:
                    pg.mouse.click(pos['x'], pos['y'])
                    time.sleep(1)
                    pg.mouse.click(pos['x'], pos['y'])
                    time.sleep(3)
                    claimed += 1
            if rnd < 19:
                pg.reload(wait_until='load', timeout=30000)
                time.sleep(10)

        b.close()
        return claimed, remaining


print("[START]")
for rnd in range(100):
    try:
        claimed, remaining = do_claim()
        TOTAL += claimed
        print(f"[Round {rnd + 1}] Claimed: {claimed} | Remaining: {remaining} | Total: {TOTAL}")
        if remaining == 0 and claimed == 0:
            print(f"\n[DONE] Total: {TOTAL}")
            break
        if claimed == 0:
            time.sleep(30)
    except Exception as e:
        print(f"Error: {e}")
        time.sleep(30)

print(f"[END] Total: {TOTAL}")
