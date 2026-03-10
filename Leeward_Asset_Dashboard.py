"""
Debug: why HOVEY_GEN and MROW_SLR_RN fail on DA and/or RT
"""

import requests
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

ERCOT_USER = "seanmlewis09@gmail.com"
ERCOT_PASS = "Football09!!"
ERCOT_SUB  = "7182acc26f2e479bafd2208300edbc37"


def ercot_auth():
    url = (
        f"https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/"
        f"B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token"
        f"?username={ERCOT_USER}&password={ERCOT_PASS}"
        f"&scope=openid+fec253ea-0d06-4272-a5e6-b478baeecd70+offline_access"
        f"&client_id=fec253ea-0d06-4272-a5e6-b478baeecd70"
        f"&response_type=id_token&grant_type=password"
    )
    resp = requests.post(url, timeout=60)
    if resp.ok:
        token = resp.json().get("access_token")
        return {"Authorization": f"Bearer {token}", "Ocp-Apim-Subscription-Key": ERCOT_SUB}
    print(f"AUTH FAILED: {resp.status_code}")
    return None


def debug_node(auths, sp_name, today):
    print(f"\n{'='*60}")
    print(f"DEBUG: {sp_name}")
    print(f"{'='*60}")

    # DA - np4-190-cd
    print(f"\n--- DA (np4-190-cd) ---")
    da_url = (
        f"https://api.ercot.com/api/public-reports/np4-190-cd/dam_stlmnt_pnt_prices"
        f"?deliveryDateFrom={today}&deliveryDateTo={today}"
        f"&settlementPoint={sp_name}"
    )
    print(f"URL: {da_url}")
    r = requests.get(f"{da_url}&page=1&size=50", headers=auths, timeout=60)
    print(f"HTTP {r.status_code}")
    if r.ok:
        result = r.json()
        meta = result.get("_meta", {})
        rows = result.get("data", [])
        fields = [f['name'] for f in result.get("fields", [])]
        print(f"Total records: {meta.get('totalRecords', '?')}")
        print(f"Rows returned: {len(rows)}")
        print(f"Fields: {fields}")
        if rows:
            df = pd.DataFrame(rows, columns=fields)
            print(f"First row: {df.iloc[0].to_dict()}")
    else:
        print(f"Error: {r.text[:300]}")

    # DA - try without settlementPoint filter to see if it's a naming issue
    print(f"\n--- DA (no filter, grep for {sp_name[:4]}) ---")
    da_url2 = (
        f"https://api.ercot.com/api/public-reports/np4-190-cd/dam_stlmnt_pnt_prices"
        f"?deliveryDateFrom={today}&deliveryDateTo={today}"
        f"&page=1&size=50000"
    )
    r2 = requests.get(da_url2, headers=auths, timeout=120)
    if r2.ok:
        result2 = r2.json()
        rows2 = result2.get("data", [])
        fields2 = [f['name'] for f in result2.get("fields", [])]
        meta2 = result2.get("_meta", {})
        print(f"Total records (all SPs): {meta2.get('totalRecords', '?')}")
        if rows2:
            df2 = pd.DataFrame(rows2, columns=fields2)
            if 'settlementPoint' in df2.columns:
                # Search for partial match
                keyword = sp_name[:4].upper()
                matches = df2[df2['settlementPoint'].str.upper().str.contains(keyword)]
                unique_matches = matches['settlementPoint'].unique().tolist()
                print(f"Partial match '{keyword}': {unique_matches}")
                if unique_matches:
                    for m in unique_matches:
                        count = len(matches[matches['settlementPoint'] == m])
                        print(f"  {m}: {count} rows")
    else:
        print(f"Bulk fetch failed: HTTP {r2.status_code}")

    # RT - np6-788-cd
    print(f"\n--- RT (np6-788-cd) ---")
    rt_url = (
        f"https://api.ercot.com/api/public-reports/np6-788-cd/lmp_node_zone_hub"
        f"?SCEDTimestampFrom={today}T00:00:00&SCEDTimestampTo={today}T23:59:59"
        f"&settlementPoint={sp_name}"
    )
    print(f"URL: {rt_url}")
    r3 = requests.get(f"{rt_url}&page=1&size=10", headers=auths, timeout=60)
    print(f"HTTP {r3.status_code}")
    if r3.ok:
        result3 = r3.json()
        meta3 = result3.get("_meta", {})
        rows3 = result3.get("data", [])
        fields3 = [f['name'] for f in result3.get("fields", [])]
        print(f"Total records: {meta3.get('totalRecords', '?')}")
        print(f"Rows returned: {len(rows3)}")
        if rows3:
            df3 = pd.DataFrame(rows3, columns=fields3)
            print(f"First row: {df3.iloc[0].to_dict()}")
            print(f"Last row: {df3.iloc[-1].to_dict()}")
    else:
        print(f"Error: {r3.text[:300]}")


if __name__ == "__main__":
    auths = ercot_auth()
    if not auths:
        exit()

    today = datetime.now(ZoneInfo('America/Chicago')).strftime('%Y-%m-%d')
    print(f"Date: {today}")

    debug_node(auths, "HOVEY_GEN", today)
    debug_node(auths, "MROW_SLR_RN", today)
    # Also test the ones that work for comparison
    debug_node(auths, "HRZN_SLR_UN1", today)
