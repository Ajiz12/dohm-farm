#!/usr/bin/env python3
"""
DOHM Farming v21 - FAST FLOW
Flow per cycle:
  Stake 0.2 -> klik "Stake DOHM" -> tunggu "Confirm & Stake" -> klik -> DONE
  -> Unstake 0.1 -> klik "Unstake DOHM" -> tunggu "Confirm & Sign" -> klik -> DONE
  -> Claim (fire & forget, no wait) -> next cycle LANGSUNG
"""
import os
import sys
import time
import re
import random
import fcntl
import hashlib
import traceback
import threading
import urllib.request
import urllib.parse
from datetime import datetime

from camoufox.sync_api import Camoufox

# ═══════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════
WALLET_PASSWORD = os.environ.get('WALLET_PASSWORD')
SEED_PHRASE     = os.environ.get('SEED_PHRASE')
if not WALLET_PASSWORD or not SEED_PHRASE:
    print("[FATAL] WALLET_PASSWORD & SEED_PHRASE wajib di-set.")
    sys.exit(1)

POINTS_TARGET = float(os.environ.get('POINTS_TARGET', '25000'))
MAX_CYCLES    = int(os.environ.get('MAX_CYCLES', '0'))
STAKE_AMOUNT  = float(os.environ.get('STAKE_AMOUNT', '0.2'))
UNSTAKE_AMOUNT= float(os.environ.get('UNSTAKE_AMOUNT', '0.1'))

JEDA_MIN = int(os.environ.get('JEDA_MIN', '5'))
JEDA_MAX = int(os.environ.get('JEDA_MAX', '15'))

# timeout tunggu tab konfirmasi
CONFIRM_TAB_TIMEOUT = int(os.environ.get('CONFIRM_TAB_TIMEOUT', '20'))
DONE_TIMEOUT        = int(os.environ.get('DONE_TIMEOUT', '10'))

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID   = os.environ.get('TELEGRAM_CHAT_ID', '')
NOTIFY_EVERY_PTS   = float(os.environ.get('NOTIFY_EVERY_PTS', '25'))

FAUCET_ENABLED   = int(os.environ.get('FAUCET_ENABLED', '1'))
FAUCET_MIN_HOURS = float(os.environ.get('FAUCET_MIN_HOURS', '6'))
FAUCET_MAX_HOURS = float(os.environ.get('FAUCET_MAX_HOURS', '12'))

AUTO_UPDATE = int(os.environ.get('AUTO_UPDATE', '0'))
UPDATE_URL  = os.environ.get('UPDATE_URL', '')

URL_STAKE     = 'https://testnet.dohm.finance/app/stake'
URL_PORTFOLIO = 'https://testnet.dohm.finance/app/portfolio'
URL_SETUP     = 'https://testnet.dohm.finance/app/setup'
LOCK_FILE     = '/tmp/dohm_farm.lock'
LOG_FILE      = '/tmp/dohm_farm.log'
HEARTBEAT_FILE= '/tmp/dohm_farm.heartbeat'
SCRIPT_PATH   = os.path.abspath(__file__)

# ═══════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════
state = {
    'stop': False,
    'initial_points': 0.0,
    'last_notify_pts': 0.0,
    'target_reached': False,
    'wallet_ok': False,
    'current_cycle': 0,
    'stake_fail_streak': 0,
    'claim_count': 0,
}

start_time = time.time()
_log_lock = threading.Lock()

# ═══════════════════════════════════════════
# LOG + NOTIF
# ═══════════════════════════════════════════
def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            with open(LOG_FILE, 'a') as f:
                f.write(line + '\n')
        except Exception:
            pass

def notify(msg):
    log(f"[NOTIF] {msg}")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            'chat_id': TELEGRAM_CHAT_ID,
            'text': msg,
            'parse_mode': 'HTML',
            'disable_web_page_preview': 'true',
        }).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception as e:
        log(f"[NOTIF] gagal: {e}")

def heartbeat(payload: str):
    try:
        with open(HEARTBEAT_FILE, 'w') as f:
            f.write(f"{datetime.now().isoformat()} | {payload}\n")
    except Exception:
        pass

# ═══════════════════════════════════════════
# AUTO UPDATE
# ═══════════════════════════════════════════
def check_and_update():
    if not AUTO_UPDATE or not UPDATE_URL:
        return False
    try:
        log(f"[UPDATE] cek {UPDATE_URL}")
        req = urllib.request.Request(UPDATE_URL, headers={'User-Agent': 'dohm-farm'})
        with urllib.request.urlopen(req, timeout=20) as r:
            remote = r.read()
        local = open(SCRIPT_PATH, 'rb').read()
        if hashlib.sha256(remote).hexdigest() == hashlib.sha256(local).hexdigest():
            log("[UPDATE] sudah terbaru")
            return False
        with open(SCRIPT_PATH + '.bak', 'wb') as f:
            f.write(local)
        with open(SCRIPT_PATH, 'wb') as f:
            f.write(remote)
        os.chmod(SCRIPT_PATH, 0o755)
        notify("🔄 <b>Auto-update</b> — restart otomatis...")
        time.sleep(2)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        log(f"[UPDATE] error: {e}")
    return False

# ═══════════════════════════════════════════
# LOCK
# ═══════════════════════════════════════════
def acquire_lock():
    f = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        log("[LOCK] Script lain masih jalan.")
        return None

# ═══════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════
def sleep_fixed(min_s=5, max_s=15, label=""):
    total = random.uniform(min_s, max_s)
    log(f"  [jeda] {label}: {total:.1f}s")
    end = time.time() + total
    while time.time() < end and not state['stop']:
        time.sleep(min(1, end - time.time()))

def get_tx_hash(page):
    try:
        txt = page.inner_text('body') or ''
        for pat in (r'(0x[a-fA-F0-9]{64})', r'([a-f0-9]{8,}\.[a-f0-9]{4,})', r'\b([a-f0-9]{64})\b'):
            m = re.findall(pat, txt)
            if m: return m[-1]
    except Exception:
        pass
    return None

def read_points_no_nav(page):
    try:
        txt = page.inner_text('body') or ''
        for pat in (r'(\d+\.?\d*)\s*pts', r'[Pp]oints?\s*[:=]?\s*(\d+\.?\d*)'):
            m = re.search(pat, txt, re.IGNORECASE)
            if m:
                return float(m.group(1))
    except Exception:
        pass
    return None

def get_points(page):
    try:
        page.goto(URL_STAKE, wait_until='load', timeout=30000)
        for _ in range(15):
            time.sleep(3)
            pts = read_points_no_nav(page)
            if pts and pts > 0:
                return pts
        return 0
    except Exception as e:
        log(f"  [get_points error] {e}")
        return 0

# ═══════════════════════════════════════════
# SELECTOR FLEKSIBEL
# ═══════════════════════════════════════════
def wait_dom_stable(page, timeout=15):
    """Tunggu DOM stabil (ga berubah 2x berturut-turut)."""
    end = time.time() + timeout
    prev_len = -1
    stable_count = 0
    while time.time() < end:
        try:
            cur_len = page.evaluate("() => document.body.innerHTML.length")
        except Exception:
            time.sleep(1)
            continue
        if cur_len == prev_len:
            stable_count += 1
            if stable_count >= 2:
                return True
        else:
            stable_count = 0
        prev_len = cur_len
        time.sleep(1)
    return False

def find_action_button(page, action, timeout=30):
    """Cari tombol aksi (stake/unstake/claim) dengan selector fleksibel."""
    patterns = {
        'stake':   [r'^Stake$', r'^Stake\s+DOHM$', r'^STAKE', r'Stake'],
        'unstake': [r'^Unstake$', r'^Unstake\s+DOHM$', r'^UNSTAKE', r'Unstake'],
        'claim':   [r'^Claim$', r'^CLAIM', r'Claim'],
    }
    end = time.time() + timeout
    while time.time() < end:
        for pat in patterns.get(action, []):
            clean = pat.strip("^$\\")
            try:
                loc = page.locator(f'button:has-text("{clean}")')
                for i in range(loc.count()):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible():
                            continue
                        txt = el.inner_text().strip()
                        if re.match(pat, txt, re.IGNORECASE):
                            return el
                    except Exception:
                        continue
            except Exception:
                pass

        try:
            all_btns = page.locator('button:visible')
            n = all_btns.count()
            for i in range(min(n, 50)):
                try:
                    el = all_btns.nth(i)
                    txt = el.inner_text().strip()
                    for pat in patterns.get(action, []):
                        if re.match(pat, txt, re.IGNORECASE):
                            return el
                except Exception:
                    continue
        except Exception:
            pass

        try:
            for sel in [
                f'button[aria-label*="{action}" i]',
                f'button[data-testid*="{action}" i]',
            ]:
                loc = page.locator(sel)
                for i in range(loc.count()):
                    el = loc.nth(i)
                    if el.is_visible():
                        return el
        except Exception:
            pass

        time.sleep(1)
    return None

def click_button_safe(page, btn, label="", force_fallback=True):
    """Klik tombol: normal → force → JS click."""
    try:
        btn.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass

    try:
        dis = btn.get_attribute('disabled', timeout=2000)
        if dis is not None:
            log(f"  [click] '{label}' disabled")
            return 'disabled'
    except Exception:
        pass

    try:
        btn.click(timeout=5000)
        return 'ok'
    except Exception as e:
        log(f"  [click] '{label}' normal click err: {e}")

    if force_fallback:
        try:
            btn.click(force=True, timeout=5000)
            log(f"  [click] '{label}' force click OK")
            return 'ok'
        except Exception as e:
            log(f"  [click] '{label}' force click err: {e}")

    try:
        btn.evaluate("el => el.click()")
        log(f"  [click] '{label}' JS click OK")
        return 'ok'
    except Exception as e:
        log(f"  [click] '{label}' JS click err: {e}")

    return 'fail'

def dump_visible_buttons(page, label=""):
    """Log semua tombol visible, buat debug."""
    try:
        btns = page.locator('button:visible')
        n = btns.count()
        texts = []
        for i in range(min(n, 20)):
            try:
                txt = btns.nth(i).inner_text().strip()[:40]
                if txt:
                    texts.append(txt)
            except Exception:
                pass
        log(f"  [{label}] visible buttons ({n}): {texts}")
    except Exception as e:
        log(f"  [{label}] dump buttons err: {e}")

def click_done_button(page, timeout=10):
    """Klik tombol DONE / OK / Close setelah confirm."""
    end = time.time() + timeout
    while time.time() < end:
        for sel in [
            'button:has-text("DONE")',
            'button:has-text("Done")',
            'button:has-text("done")',
            'button:has-text("Close")',
            'button:has-text("OK")',
            'button:has-text("Okay")',
            '[role="button"]:has-text("DONE")',
        ]:
            btn = page.locator(sel)
            for i in range(btn.count()):
                try:
                    el = btn.nth(i)
                    if el.is_visible():
                        el.scroll_into_view_if_needed()
                        el.click(timeout=3000)
                        log(f"  [done] klik '{sel.strip()}'")
                        time.sleep(2)
                        return True
                except Exception:
                    continue
        time.sleep(1)
    log("  [done] tombol DONE ga ketemu (skip)")
    return False

def wait_confirm_tab(page, mode="stake", timeout=None):
    """
    Tunggu tombol konfirmasi muncul.
    DOHM pake "Confirm & sign" untuk stake DAN unstake.
    """
    if timeout is None:
        timeout = CONFIRM_TAB_TIMEOUT

    patterns = [
        'button:has-text("Confirm & sign")',
        'button:has-text("Confirm & Sign")',
        'button:has-text("Confirm & Stake")',
        'button:has-text("Confirm & stake")',
        'button:has-text("Confirm and Sign")',
        'button:has-text("Confirm and Stake")',
        'button:has-text("CONFIRM & SIGN")',
        'button:has-text("CONFIRM & STAKE")',
    ]

    end = time.time() + timeout
    while time.time() < end:
        for sel in patterns:
            try:
                loc = page.locator(sel)
                for i in range(loc.count()):
                    el = loc.nth(i)
                    if el.is_visible():
                        txt = el.inner_text().strip()
                        log(f"  [confirm-tab] ketemu '{txt}'")
                        return el
            except Exception:
                continue
        time.sleep(1)

    log(f"  [confirm-tab] mode={mode}: ga ketemu dalam {timeout}s")
    return None

def check_and_recover_wallet(page):
    s = check_wallet_status(page)
    if s == 'connected':
        return True
    log(f"  [wallet] tiba-tiba {s}, recovery...")
    return ensure_wallet(page)

# ═══════════════════════════════════════════
# WALLET
# ═══════════════════════════════════════════
def check_wallet_status(page):
    try:
        # 1) cek tombol/btn dengan text bcrt (address truncated: bcrt…25pu)
        for i in range(min(page.locator('button:visible').count(), 30)):
            try:
                txt = page.locator('button:visible').nth(i).inner_text().strip()
                if 'bcrt' in txt.lower():
                    return 'connected'
            except Exception:
                pass
        # 2) cek body text — full address
        try:
            body = page.inner_text('body') or ''
        except Exception:
            return 'needs_connect'
        if re.search(r'bcrt1q[a-z0-9]{20,}', body) or re.search(r'\bbcrt\b', body):
            return 'connected'
        if re.search(r'0x[a-fA-F0-9]{40}', body):
            return 'connected'
        # 3) cek Connect wallet btn — visible = needs connect
        if page.locator('button:has-text("Connect wallet"):visible').count() > 0:
            return 'needs_connect'
        # 4) cek password input — needs unlock
        if page.locator('input[type="password"]').count() > 0:
            return 'needs_unlock'
    except Exception:
        pass
    return 'needs_connect'

def unlock_wallet(page):
    try:
        pw = page.locator('input[type="password"]')
        if pw.count() > 0:
            log("  [wallet] unlocking...")
            pw.fill(WALLET_PASSWORD)
            time.sleep(1)
            u = page.locator('button:has-text("Unlock"):visible')
            if u.count() > 0:
                u.first.click()
                time.sleep(4)
    except Exception as e:
        log(f"  [wallet] unlock err: {e}")

def restore_wallet(page):
    log("  [wallet] clicking Connect wallet...")
    try:
        page.locator('button:has-text("Connect wallet"):visible').first.click()
    except Exception:
        return False
    time.sleep(10)  # tunggu modal muncul (DOHM lambat)
    r = page.locator('button:has-text("Restore from recovery phrase"):visible')
    if r.count() == 0:
        log("  [wallet] 'Restore from recovery phrase' ga ketemu")
        return False
    r.first.click()
    time.sleep(5)
    ta = page.locator('textarea')
    if ta.count() > 0:
        ta.fill(SEED_PHRASE)
        time.sleep(0.5)
    pw = page.locator('input[type="password"]')
    if pw.count() > 0:
        pw.fill(WALLET_PASSWORD)
        time.sleep(0.5)
    rb = page.locator('button:has-text("Restore"):visible')
    if rb.count() > 0:
        rb.first.click()
        time.sleep(15)  # tunggu restore selesai
    page.goto(URL_STAKE, wait_until='load', timeout=30000)
    time.sleep(10)
    return check_wallet_status(page) == 'connected'

def ensure_wallet(page):
    s = check_wallet_status(page)
    log(f"  [wallet] status: {s}")
    if s == 'connected':
        return True
    if s == 'needs_unlock':
        unlock_wallet(page)
        return check_wallet_status(page) == 'connected'
    if restore_wallet(page):
        return True
    unlock_wallet(page)
    return check_wallet_status(page) == 'connected'

# ═══════════════════════════════════════════
# FORM
# ═══════════════════════════════════════════
def fill_amount(page, amount):
    inp = page.locator('input:visible:not([disabled])').first
    if inp.count() == 0:
        return 'none'
    inp.click(); inp.fill(''); time.sleep(0.3)
    inp.fill(str(amount)); time.sleep(2)
    return inp.input_value()

def verify_tab(page):
    a = page.locator('button[aria-selected="true"]').first
    return a.inner_text().strip() if a.count() > 0 else 'none'

def retry_action(fn, tries=3, base_delay=5, label=""):
    for i in range(tries):
        if state['stop']:
            return 'stopped'
        try:
            r = fn()
            if r not in ('failed-3x', 'no-confirm', None):
                return r
            log(f"  [retry:{label}] {i+1}/{tries} -> {r}")
        except Exception as e:
            log(f"  [retry:{label}] {i+1}/{tries} err: {e}")
            if 'closed' in str(e).lower() or 'target page' in str(e).lower():
                return 'browser-dead'
        if i < tries - 1:
            wait = base_delay * (2 ** i)
            log(f"  [retry:{label}] tunggu {wait}s")
            time.sleep(wait)
    return 'failed-3x'

# ═══════════════════════════════════════════
# ACTIONS - FLOW BARU
# ═══════════════════════════════════════════
def get_sohm_balance(page):
    try:
        txt = page.inner_text('body') or ''
        for pat in (r'(\d+\.?\d*)\s*sDOHM', r'[Bb]alance\s+(\d+\.?\d*)\s*sDOHM'):
            m = re.search(pat, txt, re.IGNORECASE)
            if m: return float(m.group(1))
    except Exception:
        pass
    return 0

def do_stake(page, amount=0.2):
    """
    Flow: isi amount -> klik "Stake DOHM" -> tunggu "Confirm & Stake" -> klik -> DONE
    """
    page.goto(URL_STAKE, wait_until='load', timeout=30000)
    wait_dom_stable(page, timeout=15)

    if not check_and_recover_wallet(page):
        log("  [stake] wallet ga bisa recover")
        return 'failed-3x'

    for attempt in range(3):
        # pastikan tab Stake
        active = verify_tab(page)
        if active != 'Stake':
            tab = find_action_button(page, 'stake', timeout=10)
            if tab:
                try:
                    tab.click(); time.sleep(3)
                except Exception:
                    pass
            active = verify_tab(page)
        if active != 'Stake':
            log(f"  [stake] attempt {attempt+1}: wrong tab '{active}'")
            time.sleep(3); continue

        # isi amount
        val = fill_amount(page, amount)
        log(f"  [stake] input: '{val}'")

        # cari tombol "Stake DOHM" (bukan tab "Stake")
        log(f"  [stake] attempt {attempt+1}: cari tombol submit...")
        submit = None
        for sel in [
            'button:has-text("Stake DOHM"):visible',
            'button:has-text("Stake dohm"):visible',
            'button:has-text("STAKE DOHM"):visible',
        ]:
            s = page.locator(sel)
            for i in range(s.count()):
                el = s.nth(i)
                try:
                    if el.is_visible():
                        txt = el.inner_text().strip()
                        if 'unstake' not in txt.lower():
                            submit = el
                            break
                except Exception:
                    continue
            if submit: break

        if not submit:
            log(f"  [stake] attempt {attempt+1}: tombol Stake DOHM ga ketemu")
            dump_visible_buttons(page, "stake")
            time.sleep(3); continue

        r = click_button_safe(page, submit, "stake-submit")
        if r != 'ok':
            log(f"  [stake] attempt {attempt+1}: click submit gagal ({r})")
            time.sleep(3); continue

        # tunggu tab konfirmasi "Confirm & Stake"
        log(f"  [stake] tunggu tab Confirm & Stake...")
        confirm_btn = wait_confirm_tab(page, mode="stake", timeout=CONFIRM_TAB_TIMEOUT)
        if not confirm_btn:
            log(f"  [stake] attempt {attempt+1}: tab Confirm ga muncul")
            dump_visible_buttons(page, "stake-confirm")
            time.sleep(3); continue

        # klik "Confirm & Stake"
        r = click_button_safe(page, confirm_btn, "stake-confirm")
        if r != 'ok':
            log(f"  [stake] attempt {attempt+1}: klik Confirm gagal ({r})")
            time.sleep(3); continue

        time.sleep(2)

        # klik DONE
        click_done_button(page, timeout=DONE_TIMEOUT)
        log(f"  [stake] ✅ done")
        return 'done'

    return 'failed-3x'

def do_unstake(page, amount=0.1):
    """
    Flow: pindah tab Unstake -> isi amount -> klik "Unstake DOHM"
          -> tunggu "Confirm & Sign" -> klik -> DONE
    """
    page.goto(URL_STAKE, wait_until='load', timeout=30000)
    wait_dom_stable(page, timeout=15)

    if not check_and_recover_wallet(page):
        return 'failed-3x'

    # pindah ke tab Unstake
    active = verify_tab(page)
    if active != 'Unstake':
        tab = find_action_button(page, 'unstake', timeout=10)
        if tab:
            try:
                tab.click(); time.sleep(3)
            except Exception:
                pass

    # cek balance
    bal = get_sohm_balance(page)
    if bal <= 0:
        log(f"  [unstake] no sDOHM ({bal})")
        return 'skip'
    amt = amount if amount < bal else round(max(bal - 0.001, 0), 4)
    if amt <= 0:
        return 'skip'
    log(f"  [unstake] sDOHM: {bal:.4f}, amount: {amt}")

    for attempt in range(3):
        active = verify_tab(page)
        if active != 'Unstake':
            tab = find_action_button(page, 'unstake', timeout=10)
            if tab:
                try:
                    tab.click(); time.sleep(3)
                except Exception:
                    pass
            active = verify_tab(page)
        if active != 'Unstake':
            log(f"  [unstake] attempt {attempt+1}: wrong tab '{active}'")
            time.sleep(3); continue

        val = fill_amount(page, amt)
        log(f"  [unstake] input: '{val}'")

        # cari tombol "Unstake DOHM"
        submit = None
        for sel in [
            'button:has-text("Unstake DOHM"):visible',
            'button:has-text("Unstake dohm"):visible',
            'button:has-text("UNSTAKE DOHM"):visible',
        ]:
            s = page.locator(sel)
            for i in range(s.count()):
                el = s.nth(i)
                try:
                    if el.is_visible():
                        submit = el
                        break
                except Exception:
                    continue
            if submit: break

        if not submit:
            log(f"  [unstake] attempt {attempt+1}: tombol Unstake DOHM ga ketemu")
            dump_visible_buttons(page, "unstake")
            time.sleep(3); continue

        r = click_button_safe(page, submit, "unstake-submit")
        if r != 'ok':
            log(f"  [unstake] attempt {attempt+1}: click submit gagal ({r})")
            time.sleep(3); continue

        # tunggu tab konfirmasi "Confirm & Sign"
        log(f"  [unstake] tunggu tab Confirm & Sign...")
        confirm_btn = wait_confirm_tab(page, mode="unstake", timeout=CONFIRM_TAB_TIMEOUT)
        if not confirm_btn:
            log(f"  [unstake] attempt {attempt+1}: tab Confirm ga muncul")
            dump_visible_buttons(page, "unstake-confirm")
            time.sleep(3); continue

        r = click_button_safe(page, confirm_btn, "unstake-confirm")
        if r != 'ok':
            log(f"  [unstake] attempt {attempt+1}: klik Confirm gagal ({r})")
            time.sleep(3); continue

        time.sleep(2)

        # klik DONE
        click_done_button(page, timeout=DONE_TIMEOUT)
        log(f"  [unstake] ✅ done")
        return 'done'

    return 'failed-3x'

def do_claim(page):
    """
    Flow: klik semua tombol Claim (fire & forget).
    Ga tunggu settle. Pending di mempool dibiarkan.
    """
    try:
        page.goto(URL_PORTFOLIO, wait_until='load', timeout=30000)
        wait_dom_stable(page, timeout=10)
    except Exception as e:
        log(f"  [claim] goto err: {e}")
        return 'skip'

    if not check_and_recover_wallet(page):
        return 'failed-3x'

    total = 0
    for rnd in range(5):
        btn = find_action_button(page, 'claim', timeout=5)
        if not btn:
            break

        log(f"  [claim] round {rnd+1}: ketemu tombol claim")

        clicked_this_round = 0
        for _ in range(5):
            b = find_action_button(page, 'claim', timeout=3)
            if not b:
                break
            r = click_button_safe(page, b, f"claim-{total+1}")
            if r != 'ok':
                break
            total += 1
            clicked_this_round += 1
            log(f"  [claim] bond #{total} clicked (NO WAIT)")
            time.sleep(2)

            # kadang muncul confirm/DONE setelah klik claim
            # cek sebentar, klik kalau ada
            confirm_btn = wait_confirm_tab(page, mode="unstake", timeout=3)
            if confirm_btn:
                click_button_safe(page, confirm_btn, "claim-confirm")
                time.sleep(1)
                click_done_button(page, timeout=3)

        if clicked_this_round == 0:
            break

        # cek apakah masih ada claim button (round berikutnya)
        if not find_action_button(page, 'claim', timeout=2):
            break

        try:
            page.reload(wait_until='load', timeout=30000)
            time.sleep(5)
        except Exception:
            break

    log(f"  [claim] total: {total} (fire & forget)")
    return 'done' if total > 0 else 'skip'

# ═══════════════════════════════════════════
# FAUCET
# ═══════════════════════════════════════════
def try_claim_faucet(page):
    try:
        log(f"[FAUCET] buka {URL_SETUP}")
        page.goto(URL_SETUP, wait_until='load', timeout=30000)
        for _ in range(15):
            time.sleep(2)
            try:
                if page.locator('button:visible').count() > 0:
                    break
            except Exception:
                pass
        ensure_wallet(page)
        time.sleep(2)

        link_menu = None
        for sel in ['a:has-text("Link")', 'button:has-text("Link")', 'text="Link"']:
            loc = page.locator(sel)
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    link_menu = loc.nth(i)
                    break
            if link_menu: break

        if not link_menu:
            log("[FAUCET] menu 'Link' ga ketemu")
            return 'not-available'
        log("[FAUCET] klik menu Link")
        link_menu.click()
        time.sleep(3)

        wl = None
        for sel in ['button:has-text("wallet Linked")', 'button:has-text("Wallet Linked")', 'text=/wallet\\s+Linked/i']:
            loc = page.locator(sel)
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    wl = loc.nth(i)
                    break
            if wl: break

        if wl:
            log("[FAUCET] klik 'wallet Linked'")
            wl.click()
            time.sleep(3)

        cont = None
        for sel in ['button:has-text("Continue")', 'button:has-text("CONTINUE")']:
            loc = page.locator(sel)
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    cont = loc.nth(i)
                    break
            if cont: break

        if cont:
            log("[FAUCET] klik Continue")
            cont.click()
            time.sleep(4)

        time.sleep(3)
        claimed = False

        for label, sel in [("Get BTC", 'button:has-text("Get BTC")'), ("Get frBTC", 'button:has-text("Get frBTC")')]:
            btn = page.locator(sel)
            if btn.count() > 0 and btn.first.is_visible():
                log(f"[FAUCET] klik {label}")
                try:
                    btn.first.scroll_into_view_if_needed()
                    btn.first.click()
                    time.sleep(3)
                    # tunggu confirm tab
                    confirm_btn = wait_confirm_tab(page, mode="stake", timeout=15)
                    if confirm_btn:
                        click_button_safe(page, confirm_btn, "faucet-confirm")
                        time.sleep(2)
                        click_done_button(page, timeout=5)
                        claimed = True
                    elif get_tx_hash(page):
                        claimed = True
                    time.sleep(3)
                except Exception as e:
                    log(f"[FAUCET] {label} err: {e}")

        return 'claimed' if claimed else 'not-available'
    except Exception as e:
        log(f"[FAUCET] exception: {e}")
        return 'error'

# ═══════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════
def run_session():
    global start_time
    start_time = time.time()

    log(f"[INIT] DOHM Farm v21 FAST FLOW | {datetime.now()}")
    log(f"[INIT] Target: {POINTS_TARGET} pts | Jeda: {JEDA_MIN}-{JEDA_MAX}s")
    notify(f"🚀 <b>DOHM Farm v21 FAST</b>\nTarget: {POINTS_TARGET} pts")

    state['stop'] = False
    state['target_reached'] = False
    state['wallet_ok'] = False
    state['current_cycle'] = 0
    state['stake_fail_streak'] = 0
    state['claim_count'] = 0

    with Camoufox(headless=True) as browser:
        page = browser.new_page()

        log("[INIT] buka halaman stake...")
        page.goto(URL_STAKE, wait_until='load', timeout=30000)
        time.sleep(5)

        log("[INIT] ensure wallet...")
        state['wallet_ok'] = ensure_wallet(page)
        if not state['wallet_ok']:
            log("[INIT] WARNING: wallet ga connect")
            notify("⚠️ <b>Wallet gagal connect</b>")

        state['initial_points'] = get_points(page)
        state['last_notify_pts'] = state['initial_points']
        log(f"[INIT] Points awal: {state['initial_points']:.2f}")

        next_faucet_check = time.time() + random.uniform(30, 120)

        cycle = 0
        while not state['stop'] and not state['target_reached']:
            cycle += 1
            state['current_cycle'] = cycle
            if MAX_CYCLES > 0 and cycle > MAX_CYCLES:
                log(f"[END] max cycles {MAX_CYCLES}")
                break

            log(f"\n{'='*60}")
            log(f"CYCLE {cycle} | {datetime.now().strftime('%H:%M:%S')}")
            log(f"{'='*60}")

            # cek page hidup
            try:
                if page.is_closed():
                    log("[INIT] ⚠️ page closed, break")
                    break
            except Exception:
                log("[INIT] ⚠️ page error, break")
                break

            pts = get_points(page)
            heartbeat(f"cycle={cycle} pts={pts:.2f}")

            # notif
            if pts - state['last_notify_pts'] >= NOTIFY_EVERY_PTS:
                gained = pts - state['initial_points']
                notify(f"📈 <b>+{int(pts - state['last_notify_pts'])} pts</b>\n"
                       f"Total: <b>{pts:.2f}</b> / {POINTS_TARGET}\n"
                       f"Progress: {pts/POINTS_TARGET*100:.1f}%\n"
                       f"Cycle: {cycle}\n"
                       f"Claim done: {state['claim_count']}\n"
                       f"Gained: {gained:+.2f} pts")
                state['last_notify_pts'] = pts

            if pts >= POINTS_TARGET:
                notify(f"🎯 <b>TARGET TERCAPAI!</b>\nPoints: <b>{pts:.2f}</b>")
                state['target_reached'] = True
                break

            log(f"  pts: {pts:.2f}")

            # faucet
            if FAUCET_ENABLED and time.time() >= next_faucet_check:
                log("[FAUCET] waktunya cek faucet")
                res = try_claim_faucet(page)
                log(f"[FAUCET] result: {res}")
                if res == 'claimed':
                    notify("💧 <b>Faucet claimed!</b>")
                    next_faucet_check = time.time() + random.uniform(FAUCET_MIN_HOURS*3600, FAUCET_MAX_HOURS*3600)
                elif res == 'cooldown':
                    next_faucet_check = time.time() + 2*3600
                elif res == 'not-available':
                    next_faucet_check = time.time() + 3*3600
                else:
                    next_faucet_check = time.time() + 3600
                try:
                    page.goto(URL_STAKE, wait_until='load', timeout=30000)
                    time.sleep(3)
                except Exception:
                    pass

            # ── STAKE ──
            log(f"  [1/3] Stake {STAKE_AMOUNT} DOHM...")
            r1 = retry_action(lambda: do_stake(page, STAKE_AMOUNT), tries=3, label="stake")
            log(f"  -> {r1}")
            if r1 == 'browser-dead':
                log("[SEQ] browser mati, exit")
                break
            if r1 != 'done':
                state['stake_fail_streak'] += 1
                wait_extra = min(30 * state['stake_fail_streak'], 180)
                log(f"  [!] stake gagal ({r1}), streak={state['stake_fail_streak']}, tunggu {wait_extra}s")
                try:
                    page.goto(URL_STAKE, wait_until='load', timeout=30000)
                    time.sleep(3)
                except Exception:
                    pass
                time.sleep(wait_extra)
                continue
            state['stake_fail_streak'] = 0

            sleep_fixed(JEDA_MIN, JEDA_MAX, "setelah stake")

            # ── UNSTAKE ──
            log(f"  [2/3] Unstake {UNSTAKE_AMOUNT} sDOHM...")
            r2 = retry_action(lambda: do_unstake(page, UNSTAKE_AMOUNT), tries=3, label="unstake")
            log(f"  -> {r2}")
            if r2 == 'browser-dead':
                break
            if r2 == 'skip':
                log(f"  no sDOHM, langsung claim")

            sleep_fixed(JEDA_MIN, JEDA_MAX, "setelah unstake")

            # ── CLAIM (fire & forget) ──
            log(f"  [3/3] Claim (fire & forget)...")
            r3 = retry_action(lambda: do_claim(page), tries=2, label="claim")
            log(f"  -> {r3}")
            if r3 == 'browser-dead':
                break
            if r3 == 'done':
                state['claim_count'] += 1

            pts_now = get_points(page)
            log(f"  cycle {cycle} done. pts={pts_now:.2f} → next cycle")

        log("[END] session selesai")

# ═══════════════════════════════════════════
# SUPERVISOR
# ═══════════════════════════════════════════
if __name__ == '__main__':
    lock = acquire_lock()
    if not lock:
        sys.exit(1)

    restart_count = 0
    while True:
        try:
            run_session()
            if state['target_reached']:
                log("[SUPERVISOR] Target reached. Exit.")
                notify("✅ <b>DOHM Farm selesai</b>")
                break
            else:
                log("[SUPERVISOR] Session ended, restart in 30s...")
                time.sleep(30)
        except KeyboardInterrupt:
            log("[SUPERVISOR] Ctrl+C, exit.")
            state['stop'] = True
            break
        except Exception as e:
            restart_count += 1
            log(f"[SUPERVISOR] CRASH #{restart_count}: {e}")
            log(traceback.format_exc())
            notify(f"🔴 <b>Crash #{restart_count}</b>\n<code>{str(e)[:200]}</code>")
            state['stop'] = True
            time.sleep(5)
            backoff = min(30 * restart_count, 300)
            log(f"[SUPERVISOR] Restart in {backoff}s...")
            time.sleep(backoff)
    log("[SUPERVISOR] Exiting.")