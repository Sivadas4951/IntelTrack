import json
import os
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

app = FastAPI(title="RivalIQ Mock Competitor Sandbox")

STATE_FILE = "mock_sites/state.json"

def load_state():
    if not os.path.exists(STATE_FILE):
        raise FileNotFoundError(f"{STATE_FILE} missing.")
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# --- XHR/JSON API ENDPOINTS (Rung 2 Interception) ---

@app.get("/api/v1/catalog/{market}")
async def get_catalog_json(market: str):
    state = load_state()
    if market in state:
        return JSONResponse(content=state[market])
    return JSONResponse(status_code=404, content={"error": "Market not found"})


# --- HTML PAGE ENDPOINTS (Rung 1 & 3 Scrapes) ---

@app.get("/isp/plans", response_class=HTMLResponse)
async def isp_page():
    state = load_state()
    data = state["isp"]
    cosmetic = state["cosmetic"]
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>SpeedFiber Plans - Pune</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; background: #f4f6f9; }}
            .card {{ background: white; padding: 24px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 500px; }}
            .price {{ font-size: 28px; color: #2c3e50; font-weight: bold; }}
            .spec {{ margin: 10px 0; font-size: 16px; color: #555; }}
            .banner {{ background: #e74c3c; color: white; padding: 10px; margin-bottom: 20px; font-size: 14px; }}
            footer {{ margin-top: 30px; font-size: 12px; color: #888; }}
        </style>
    </head>
    <body>
        <div class="banner">{cosmetic['cookie_banner']}</div>
        <div class="card">
            <h2>SpeedFiber {data['plan_name']}</h2>
            <div class="price">₹{data['price_inr']} / month</div>
            <div class="spec"><strong>Speed:</strong> {data['speed_mbps']} Mbps</div>
            <div class="spec"><strong>FUP Data Limit:</strong> {data['fup_cap_gb']} GB</div>
            <div class="spec"><strong>Post-FUP Speed:</strong> {data['post_fup_speed_mbps']} Mbps</div>
            <div class="spec"><strong>Included OTT:</strong> {', '.join(data['ott_bundle'])}</div>
            <p><em>"{data['testimonial']}"</em></p>
        </div>
        <footer>© {cosmetic['footer_year']} SpeedFiber Broadband Pune. All rights reserved.</footer>

        <script>
            // XHR Call for Rung 2 Interception Verification[cite: 2]
            fetch('/api/v1/catalog/isp')
                .then(r => r.json())
                .then(d => console.log("Loaded ISP Catalog via XHR:", d));
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/dairy/products", response_class=HTMLResponse)
async def dairy_page():
    state = load_state()
    data = state["dairy"]
    cosmetic = state["cosmetic"]

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>FreshDairy Products</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; background: #f9fbf7; }}
            .card {{ background: white; padding: 24px; border-radius: 8px; border: 1px solid #e0e0e0; max-width: 500px; }}
            .price {{ font-size: 26px; color: #27ae60; font-weight: bold; }}
            .banner {{ background: #34495e; color: white; padding: 10px; margin-bottom: 20px; }}
        </style>
    </head>
    <body>
        <div class="banner">{cosmetic['cookie_banner']}</div>
        <div class="card">
            <h2>{data['product_name']}</h2>
            <div class="price">₹{data['price_inr']}</div>
            <p><strong>Pack Volume:</strong> {data['pack_size_ml']} ml</p>
            <p><strong>Fat Content:</strong> {data['fat_percentage']}%</p>
            <p><em>"{data['testimonial']}"</em></p>
        </div>
        <footer>© {cosmetic['footer_year']} FreshDairy Ltd.</footer>

        <script>
            fetch('/api/v1/catalog/dairy');
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/audio/earbuds", response_class=HTMLResponse)
async def audio_page():
    state = load_state()
    data = state["headphones"]
    cosmetic = state["cosmetic"]

    anc_status = "Yes" if data['anc_enabled'] else "No"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>SoundMax Audio Store</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; background: #fafafa; }}
            .card {{ background: white; padding: 24px; border-radius: 8px; border: 1px solid #ddd; max-width: 500px; }}
            .price {{ font-size: 26px; color: #e67e22; font-weight: bold; }}
        </style>
    </head>
    <body>
        <div style="background:#2c3e50; color:white; padding:10px;">{cosmetic['cookie_banner']}</div>
        <div class="card">
            <h2>{data['model_name']}</h2>
            <div class="price">₹{data['price_inr']}</div>
            <p><strong>Playtime:</strong> Up to {data['battery_hours']} Hours</p>
            <p><strong>Active Noise Cancellation:</strong> {anc_status}</p>
            <p><strong>Warranty:</strong> {data['warranty_months']} Months</p>
            <p><em>"{data['testimonial']}"</em></p>
        </div>
        <footer>© {cosmetic['footer_year']} SoundMax Audio India.</footer>

        <script>
            fetch('/api/v1/catalog/headphones');
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


# --- ADMIN FLIP PANEL (DEMO CONTROL CENTER)[cite: 1, 2] ---

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel():
    state = load_state()
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>RivalIQ Admin Flip Panel</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 30px; background: #1e1e2f; color: #fff; }}
            .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
            .box {{ background: #27293d; padding: 20px; border-radius: 8px; }}
            button {{ padding: 12px 20px; font-weight: bold; border: none; border-radius: 4px; cursor: pointer; margin-top: 10px; width: 100%; }}
            .stealth {{ background: #e74c3c; color: white; }}
            .noise {{ background: #f39c12; color: white; }}
            .reset {{ background: #27ae60; color: white; }}
            pre {{ background: #000; padding: 10px; border-radius: 4px; overflow-x: auto; color: #00ff00; }}
        </style>
    </head>
    <body>
        <h1>RivalIQ Demo Admin Flip Panel</h1>
        <p>Trigger stealth market changes or cosmetic layout noise live during the demonstration[cite: 1, 2].</p>

        <div class="grid">
            <div class="box">
                <h3>Stealth Flips (Same Price, Reduced Value)[cite: 1, 2]</h3>
                <form action="/admin/flip" method="post">
                    <input type="hidden" name="action" value="cut_isp_fup">
                    <button class="stealth" type="submit">Cut ISP FUP (3300GB → 1500GB)</button>
                </form>
                <form action="/admin/flip" method="post">
                    <input type="hidden" name="action" value="shrink_dairy">
                    <button class="stealth" type="submit">Shrink Dairy Pack (500ml → 450ml)</button>
                </form>
                <form action="/admin/flip" method="post">
                    <input type="hidden" name="action" value="cut_earbud_battery">
                    <button class="stealth" type="submit">Cut Earbud Battery (40h → 30h)</button>
                </form>
            </div>

            <div class="box">
                <h3>Cosmetic Noise Flips (Ignored by Verifier)[cite: 1, 2]</h3>
                <form action="/admin/flip" method="post">
                    <input type="hidden" name="action" value="change_cookie_banner">
                    <button class="noise" type="submit">Mutate Cookie Banner Text</button>
                </form>
                <form action="/admin/flip" method="post">
                    <input type="hidden" name="action" value="update_footer_year">
                    <button class="noise" type="submit">Update Footer Timestamp</button>
                </form>
                <form action="/admin/flip" method="post">
                    <input type="hidden" name="action" value="rotate_testimonial">
                    <button class="noise" type="submit">Rotate Testimonial Text</button>
                </form>
                <hr style="margin-top: 20px; border-color: #444;">
                <form action="/admin/flip" method="post">
                    <input type="hidden" name="action" value="reset">
                    <button class="reset" type="submit">Reset All State to Baseline</button>
                </form>
            </div>
        </div>

        <h3 style="margin-top: 30px;">Current State Preview</h3>
        <pre>{json.dumps(state, indent=2)}</pre>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.post("/admin/flip")
async def handle_flip(action: str = Form(...)):
    state = load_state()

    # Stealth Flips[cite: 1, 2]
    if action == "cut_isp_fup":
        state["isp"]["fup_cap_gb"] = 1500
    elif action == "shrink_dairy":
        state["dairy"]["pack_size_ml"] = 450
    elif action == "cut_earbud_battery":
        state["headphones"]["battery_hours"] = 30

    # Cosmetic Flips[cite: 1, 2]
    elif action == "change_cookie_banner":
        state["cosmetic"]["cookie_banner"] = "Updated Privacy Policy: We value your data security."
    elif action == "update_footer_year":
        state["cosmetic"]["footer_year"] = "2027"
    elif action == "rotate_testimonial":
        state["isp"]["testimonial"] = "Great connectivity across central Pune!"
        state["dairy"]["testimonial"] = "Freshness delivered every morning."
        state["headphones"]["testimonial"] = "Crystal clear bass and highs!"

    # Reset[cite: 2]
    elif action == "reset":
        state = {
            "isp": {
                "plan_name": "Fiber Ultra",
                "price_inr": 799,
                "speed_mbps": 200,
                "fup_cap_gb": 3300,
                "post_fup_speed_mbps": 10,
                "ott_bundle": ["Disney+ Hotstar", "SonyLIV"],
                "testimonial": "Fastest internet in Pune! - Rahul S."
            },
            "dairy": {
                "product_name": "Fresh Whole Milk",
                "price_inr": 30,
                "pack_size_ml": 500,
                "fat_percentage": 4.5,
                "testimonial": "Pure and fresh daily milk."
            },
            "headphones": {
                "model_name": "SoundMax Pro TWS",
                "price_inr": 1999,
                "battery_hours": 40,
                "anc_enabled": True,
                "warranty_months": 12,
                "testimonial": "Amazing noise cancellation!"
            },
            "cosmetic": {
                "cookie_banner": "We use cookies to enhance your browsing experience.",
                "footer_year": "2026"
            }
        }

    save_state(state)
    return HTMLResponse(content="<script>window.location.href='/admin';</script>")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)