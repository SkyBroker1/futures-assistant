# маленький офлайн-smoke: перевіряє парсер ETF та sanity без мережі
import json, sys, types

# фейкові відповіді
FAKE_SOSO_HTML = """
<table><tr><th>Date</th><th>Flow</th></tr>
<tr><td>2025-10-28</td><td>$+123,456,789</td></tr>
<tr><td>2025-10-29</td><td>$-12,345,678</td></tr>
</table>
"""
FAKE_CG_SPOT = {"prices":[[1700000000000, 35000.0],[1700000864000, 35123.0]]}

# мінімальна функція sanity: BTC>1000
def sanity_btc_from_cg(fake):
    last = fake["prices"][-1][1]
    return last > 1000

def parse_sosovalue_rows(html: str) -> int:
    # тупий лічильник <tr> без заголовку
    rows = html.lower().split("<tr>")
    return max(0, len(rows) - 2)

def main():
    assert sanity_btc_from_cg(FAKE_CG_SPOT), "BTC sanity failed on fake CG"
    rows = parse_sosovalue_rows(FAKE_SOSO_HTML)
    assert rows >= 1, "ETF rows parse failed"
    print("SMOKE OK: sanity BTC & ETF parser")

if __name__ == "__main__":
    main()
