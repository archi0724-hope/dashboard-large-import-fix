from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, g, request, send_file, session, redirect

from .integration import get_session_agent, session_agents_lock
import secrets


app = Flask(__name__)
app.secret_key = os.getenv("HOSPKART_SESSION_SECRET") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_NAME="hospkart_session", SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

@app.before_request
def prepare_agent():
    if request.endpoint in {"health", "debug_config", "model_test", "chat", "download_export"}:
        session.setdefault("chat_id", secrets.token_urlsafe(24))
        g.agent = get_session_agent(session["chat_id"])
ROOT_DIR = Path(__file__).resolve().parent
LOGO_CANDIDATES = (
    ROOT_DIR / "hospkart-logo.jpg",
    ROOT_DIR / "hospkart-hero-fallback.jpg",
)
LOGO_PATH = next((p for p in LOGO_CANDIDATES if p.exists()), LOGO_CANDIDATES[-1])
HERO_CANDIDATES = (
    ROOT_DIR / "hospkart-hero.png",
    ROOT_DIR / "hospkart-hero-fallback.jpg",
)
HERO_PATH = next((p for p in HERO_CANDIDATES if p.exists()), HERO_CANDIDATES[0])
EXPORTS_DIR = ROOT_DIR / "exports"

# Original QuoteSarthi interface embedded inside Vendor AI Assistant.

CHAT_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QuoteSarthi AI</title>
  <link rel="icon" type="image/jpeg" href="__CHAT_PREFIX__/assets/hospkart-logo">
  <style>
    :root {
      --bg: #f5f8ff;
      --panel: #ffffff;
      --panel-2: #f9fbff;
      --border: #d8e4f4;
      --text: #173a70;
      --muted: #6b7d95;
      --accent: #f08a00;
      --accent-strong: #d66d00;
      --gold: #f5c84c;
      --blue-strong: #1e4f92;
      --success: #18a957;
      --danger: #dc2626;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, Segoe UI, Roboto, Arial, sans-serif;
      background: linear-gradient(180deg, #fff6d9 0%, #f7fbff 26%, #eef4ff 100%);
      color: var(--text);
      height: 100vh;
      overflow: hidden;
    }
    .hidden { display: none !important; }
    .landing-page {
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
      background:
        radial-gradient(circle at 12% 10%, rgba(240, 138, 0, 0.16) 0%, rgba(240, 138, 0, 0) 36%),
        radial-gradient(circle at 88% 86%, rgba(30, 79, 146, 0.14) 0%, rgba(30, 79, 146, 0) 38%),
        linear-gradient(160deg, #fff7e5 0%, #f6faff 48%, #eef5ff 100%);
    }
    .landing-card {
      width: min(1120px, 100%);
      border-radius: 24px;
      border: 1px solid #d6e3f5;
      background: linear-gradient(180deg, #ffffff 0%, #f9fbff 100%);
      box-shadow: 0 18px 45px rgba(18, 54, 102, 0.18);
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 18px;
      padding: 26px;
      align-items: center;
    }
    .landing-image-wrap {
      border: 1px solid #e2ecf7;
      border-radius: 18px;
      background: #ffffff;
      padding: 10px;
      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.75);
    }
    .landing-image-wrap img {
      width: 100%;
      border-radius: 12px;
      display: block;
      object-fit: cover;
      max-height: 520px;
    }
    .landing-content {
      display: flex;
      flex-direction: column;
      gap: 12px;
      padding-right: 6px;
    }
    .landing-chip {
      align-self: flex-start;
      border-radius: 999px;
      border: 1px solid #f2c47a;
      background: #fff2d3;
      color: #9c5f00;
      font-size: 12px;
      font-weight: 700;
      padding: 7px 11px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .landing-title {
      margin: 0;
      font-size: clamp(34px, 4vw, 48px);
      line-height: 1.08;
      color: #173f78;
      letter-spacing: 0.3px;
    }
    .landing-highlight { color: #ee840d; }
    .landing-subtitle {
      margin: 0;
      color: #2a588f;
      font-size: clamp(16px, 2.1vw, 20px);
      font-weight: 600;
    }
    .landing-copy {
      margin: 0;
      color: #5f7493;
      font-size: 14px;
      line-height: 1.6;
      max-width: 480px;
    }
    .landing-cta {
      border: 0;
      border-radius: 13px;
      background: linear-gradient(145deg, var(--accent), var(--accent-strong));
      color: #fff;
      font-size: 16px;
      font-weight: 700;
      letter-spacing: 0.3px;
      padding: 13px 24px;
      cursor: pointer;
      box-shadow: 0 10px 24px rgba(240, 138, 0, 0.34);
      width: fit-content;
    }
    .landing-cta:hover {
      transform: translateY(-1px);
      filter: brightness(1.03);
    }
    .shell {
      max-width: 1180px;
      margin: 0 auto;
      padding: 20px;
      height: 100vh;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      background: linear-gradient(180deg, #ffffff 0%, #fbfdff 100%);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px 16px;
      box-shadow: 0 10px 24px rgba(30, 79, 146, 0.08);
      flex-shrink: 0;
    }
    .title-wrap h1 {
      margin: 0;
      font-size: 28px;
      letter-spacing: 0.2px;
      color: #174786;
    }
    .brand-row {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .brand-logos {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 6px 8px;
      border: 1px solid #e4ecf7;
      background: linear-gradient(180deg, #ffffff 0%, #f7fbff 100%);
      border-radius: 14px;
    }
    .brand-logo {
      width: 64px;
      height: 64px;
      border-radius: 10px;
      object-fit: contain;
      border: 1px solid #e0e9f8;
      background: #fff;
      box-shadow: 0 5px 14px rgba(23, 58, 112, 0.12);
      transition: transform 180ms ease, box-shadow 180ms ease;
    }
    .brand-logo-main {
      width: 98px;
      height: 64px;
      padding: 4px 6px;
      border-color: #f2cb80;
      background: #fffef9;
    }
    .brand-logo:hover {
      transform: translateY(-2px) scale(1.02);
      box-shadow: 0 10px 22px rgba(244, 171, 52, 0.35);
    }
    .title-wrap p {
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 13px;
    }
    .status-wrap {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 8px 12px;
      font-size: 12px;
      color: var(--muted);
      background: #f5f9ff;
    }
    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: #afbed3;
      flex-shrink: 0;
    }
    .dot.ok { background: var(--success); }
    .dot.err { background: var(--danger); }
    .ghost-btn {
      border: 1px solid var(--border);
      background: #ffffff;
      color: var(--blue-strong);
      border-radius: 10px;
      padding: 8px 10px;
      font-size: 12px;
      cursor: pointer;
    }
    .ghost-btn:hover {
      background: #f7fbff;
      border-color: #c9d9ee;
    }

    .layout {
      display: grid;
      grid-template-columns: 1fr 300px;
      gap: 14px;
      flex: 1;
      min-height: 0;
    }
    .chat-panel, .side-panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      box-shadow: 0 10px 22px rgba(30, 79, 146, 0.08);
      min-height: 0;
    }
    .chat-panel {
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .chat-box {
      flex: 1;
      min-height: 240px;
      overflow-y: auto;
      padding: 14px;
      background:
        radial-gradient(circle at 100% 0%, rgba(30, 79, 146, 0.06) 0%, rgba(30, 79, 146, 0) 35%),
        radial-gradient(circle at 0% 100%, rgba(240, 138, 0, 0.08) 0%, rgba(240, 138, 0, 0) 32%);
      scroll-behavior: smooth;
    }
    .chat-box::-webkit-scrollbar { width: 10px; }
    .chat-box::-webkit-scrollbar-thumb {
      background: #d8e4f4;
      border-radius: 999px;
      border: 2px solid #f8fbff;
    }
    .message {
      margin: 8px 0;
      display: flex;
      gap: 8px;
      align-items: flex-start;
    }
    .message.user-msg {
      flex-direction: row-reverse;
    }
    .avatar {
      width: 30px;
      height: 30px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      font-size: 10px;
      font-weight: 700;
      color: #ffffff;
      flex-shrink: 0;
    }
    .avatar.user { background: linear-gradient(145deg, #f7a11b, var(--accent-strong)); }
    .avatar.bot { background: var(--blue-strong); border: 1px solid #4175b9; }
    .bubble {
      width: fit-content;
      max-width: 82%;
      border-radius: 10px;
      padding: 9px 10px;
      border: 1px solid #d7e4f8;
      background: var(--panel-2);
      min-width: 0;
      box-shadow: 0 2px 8px rgba(25, 54, 99, 0.06);
    }
    .message.user-msg .bubble { margin-left: auto; }
    .meta {
      font-size: 10px;
      color: #8194ad;
      margin-top: 5px;
      text-align: right;
    }
    .user-bubble {
      background: linear-gradient(180deg, #f7a31e 0%, #ef8612 100%);
      border-color: #d97810;
      color: #ffffff;
      white-space: pre-wrap;
    }
    .plain {
      white-space: pre-wrap;
      line-height: 1.4;
      font-size: 13px;
    }
    .card {
      border: 1px solid #d9e5f6;
      border-radius: 10px;
      background: #ffffff;
      padding: 10px;
      margin-top: 8px;
    }
    .card h4 {
      margin: 0 0 8px;
      font-size: 13px;
      color: #1e4f92;
    }
    .download-links {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 8px;
    }
    .download-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid #b8d2f5;
      border-radius: 8px;
      padding: 7px 10px;
      background: #edf5ff;
      color: #1a4a88;
      font-size: 12px;
      text-decoration: none;
      font-weight: 600;
    }
    .download-btn:hover {
      background: #e3efff;
      border-color: #9dc2f2;
    }
    .kv {
      display: grid;
      grid-template-columns: 180px 1fr;
      gap: 4px 10px;
      font-size: 13px;
    }
    .k { color: #6f84a2; }
    .v { color: #183a71; word-break: break-word; }
    .badge {
      display: inline-block;
      padding: 5px 8px;
      border-radius: 999px;
      border: 1px solid #f0bc4b;
      background: #fff6d9;
      color: #a66100;
      font-size: 12px;
      margin: 2px 6px 2px 0;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 8px;
      font-size: 12px;
    }
    th, td {
      border: 1px solid #d8e4f5;
      padding: 6px 7px;
      vertical-align: top;
      text-align: left;
      word-break: break-word;
    }
    th { background: #1e4f92; color: #ffffff; }
    tr:nth-child(even) td { background: #f7fbff; }
    .bubble-actions {
      display: flex;
      justify-content: flex-end;
      margin-top: 5px;
    }
    .mini-btn {
      border: 1px solid #f0cf8f;
      background: #fff4dd;
      color: #a66100;
      padding: 4px 7px;
      border-radius: 8px;
      font-size: 11px;
      cursor: pointer;
    }
    .mini-btn:hover { background: #ffeecb; }
    .composer {
      border-top: 1px solid var(--border);
      padding: 12px;
      display: flex;
      gap: 8px;
      align-items: center;
      background: #ffffff;
      border-bottom-left-radius: 14px;
      border-bottom-right-radius: 14px;
      flex-shrink: 0;
    }
    .mode-prompt {
      border-top: 1px dashed #d4e4f8;
      padding: 12px 12px 4px;
      background: linear-gradient(180deg, #f9fbff 0%, #f4f9ff 100%);
      display: none;
      flex-direction: column;
      gap: 8px;
    }
    .mode-title {
      font-size: 12px;
      color: #2b4f80;
      font-weight: 700;
    }
    .mode-product {
      color: #154682;
      font-weight: 700;
    }
    .mode-options {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .mode-btn {
      border: 1px solid #d7e5f9;
      background: #ffffff;
      color: #1f4f90;
      border-radius: 10px;
      padding: 8px 11px;
      font-size: 12px;
      cursor: pointer;
      font-weight: 600;
      transition: all 140ms ease;
      box-shadow: 0 3px 10px rgba(31, 79, 144, 0.08);
    }
    .mode-btn:hover {
      border-color: #b7cff0;
      background: #f3f8ff;
      transform: translateY(-1px);
    }
    .mode-btn.primary {
      border-color: #f0c15d;
      background: linear-gradient(145deg, #fff9eb, #fff1cf);
      color: #9b5b00;
    }
    .mode-chip-note {
      font-size: 11px;
      color: #6280a8;
    }
    .composer input {
      flex: 1;
      border: 1px solid #d4e1f5;
      border-radius: 10px;
      background: #f9fbff;
      color: var(--text);
      padding: 12px;
      outline: none;
    }
    .composer button {
      border: 0;
      border-radius: 10px;
      background: linear-gradient(145deg, var(--accent), var(--accent-strong));
      color: white;
      padding: 11px 16px;
      cursor: pointer;
      font-weight: 600;
    }
    .composer button:hover {
      filter: brightness(1.03);
      transform: translateY(-1px);
    }
    .composer button:disabled { opacity: 0.65; cursor: not-allowed; }
    .side-panel {
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      overflow-y: auto;
      scroll-behavior: smooth;
    }
    .side-panel::-webkit-scrollbar { width: 9px; }
    .side-panel::-webkit-scrollbar-thumb {
      background: #d8e4f4;
      border-radius: 999px;
      border: 2px solid #f8fbff;
    }
    .help-card {
      border: 1px solid #d8e4f5;
      border-radius: 10px;
      background: #ffffff;
      padding: 10px;
      font-size: 13px;
      color: #204878;
    }
    .help-card h3 {
      margin: 0 0 6px;
      font-size: 14px;
    }
    .hero-banner {
      border: 1px solid #d9e4f5;
      border-radius: 10px;
      overflow: hidden;
      background: #ffffff;
      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.7);
    }
    .hero-brand-row {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 10px 12px;
      background: linear-gradient(90deg, #fdf3d8 0%, #f5f9ff 100%);
      border-bottom: 1px solid #e2ecf8;
    }
    .hero-brand-text {
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.3px;
      color: #1f4f90;
      text-transform: uppercase;
    }
    .hero-banner img {
      width: 100%;
      display: block;
      object-fit: contain;
      max-height: 250px;
      background: #ffffff;
    }
    .control-row {
      display: grid;
      gap: 5px;
      margin-top: 8px;
    }
    .control-row label { font-size: 12px; color: var(--muted); }
    .control-row input, .control-row select {
      border: 1px solid #d5e1f3;
      background: #ffffff;
      color: #1f487a;
      border-radius: 8px;
      padding: 8px;
      width: 100%;
      font-size: 13px;
    }
    .check-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 8px;
      font-size: 12px;
      color: #355b8e;
    }
    .hint { color: var(--muted); font-size: 12px; line-height: 1.45; }
    code {
      background: #fff5df;
      color: #8f5600;
      border: 1px solid #f3cc84;
      border-radius: 6px;
      padding: 2px 4px;
    }
    .typing {
      font-size: 12px;
      color: #a66a00;
      border: 1px dashed #f0bc4b;
      border-radius: 8px;
      padding: 7px 9px;
      margin-top: 8px;
      display: none;
    }
    .quick-actions {
      display: flex;
      gap: 8px;
      padding: 8px 12px 0;
      flex-wrap: wrap;
    }
    .quick-btn {
      border: 1px solid #d8e4f4;
      background: #ffffff;
      color: #1f4f90;
      border-radius: 999px;
      padding: 7px 11px;
      font-size: 12px;
      cursor: pointer;
      transition: all 150ms ease;
    }
    .quick-btn:hover {
      border-color: #f0c15d;
      background: #fff7e8;
      color: #9b5b00;
    }
    .section-title {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.6px;
      color: #6e85a5;
      margin-bottom: 6px;
    }
    .bullet-list {
      margin: 0;
      padding-left: 18px;
    }
    .bullet-list li {
      margin: 3px 0;
      color: #204777;
      font-size: 13px;
      line-height: 1.45;
    }
    .gold-strip {
      background: linear-gradient(90deg, #f5c84c 0%, #f7d776 40%, #fff6df 100%);
      border: 1px solid #f2d080;
      border-radius: 12px;
      padding: 8px 10px;
      margin-bottom: 4px;
      display: flex;
      gap: 8px;
      overflow-x: auto;
      white-space: nowrap;
      flex-shrink: 0;
    }
    .gold-chip {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid #e7b85b;
      border-radius: 999px;
      background: #fffdf4;
      color: #8f5600;
      padding: 6px 10px;
      font-size: 12px;
      font-weight: 600;
    }
    @media (max-width: 990px) {
      .landing-card {
        grid-template-columns: 1fr;
        padding: 16px;
      }
      .landing-content {
        padding-right: 0;
      }
      .layout { grid-template-columns: 1fr; }
      .side-panel { order: -1; }
      .shell { padding: 12px; }
      .hero-banner img { max-height: 170px; }
    }
  </style>
</head>
<body>
  <section id="landingPage" class="landing-page">
    <div class="landing-card">
      <div class="landing-image-wrap">
        <img src="__CHAT_PREFIX__/assets/hospkart-hero" alt="HospKart Inhouse AI Automation">
      </div>
      <div class="landing-content">
        <div class="landing-chip">BD Helper</div>
        <h1 class="landing-title">QuoteSarthi AI <span class="landing-highlight">Chatbot</span></h1>
        <p class="landing-subtitle">Make your quote journey easy.</p>
        <p class="landing-copy">
          Product se final invoice tak complete flow ek hi jagah. Faster replies, cleaner quotation handling,
          aur better conversion-ready sales support for BD team.
        </p>
        <button id="getStartedBtn" class="landing-cta">Get Started</button>
      </div>
    </div>
  </section>

  <div id="appShell" class="shell hidden">
    <div class="topbar">
      <div class="title-wrap">
        <div class="brand-row">
          <div class="brand-logos">
            <img class="brand-logo brand-logo-main" src="__CHAT_PREFIX__/assets/hospkart-logo" alt="HospKart logo">
          </div>
          <div>
            <h1>QuoteSarthi AI</h1>
            <p>From Product Name to Perfect Quote — Instantly.</p>
          </div>
        </div>
      </div>
      <div class="status-wrap">
        <button id="exportChatBtn" class="ghost-btn">Export Chat</button>
        <div class="status">
          <span id="statusDot" class="dot"></span>
          <span id="statusText">Checking backend...</span>
        </div>
      </div>
    </div>
    <div class="layout">
      <section class="chat-panel">
        <div id="chat" class="chat-box"></div>
        <div id="typing" class="typing">Agent is preparing a structured response...</div>
        <div id="modePrompt" class="mode-prompt">
          <div class="mode-title">
            Smart action choose karo for <span id="modeProductLabel" class="mode-product"></span>
          </div>
          <div class="mode-chip-note">Dataset based responses: pricing math, vendor coverage, details, quote</div>
          <div class="mode-options">
            <button class="mode-btn primary" data-mode="best_pricing_math">Best Pricing Intelligence</button>
            <button class="mode-btn" data-mode="vendor_coverage">Vendor Coverage</button>
            <button class="mode-btn" data-mode="product_details">Full Product Details</button>
            <button class="mode-btn" data-mode="create_quotation">Create Quotation</button>
          </div>
          <div class="mode-options">
            <button id="modeSkipBtn" class="mode-btn">Skip & Search Normally</button>
          </div>
        </div>
        <div class="composer">
          <input id="q" placeholder="Search by product/vendor or ask quotation with quantity and discount">
          <button id="send">Send</button>
        </div>
      </section>

      <aside class="side-panel">
        <div class="hero-banner">
          <div class="hero-brand-row">
            <div class="hero-brand-text">Official Service by HospKart AI Wing</div>
          </div>
          <img src="__CHAT_PREFIX__/assets/hospkart-hero" alt="HospKart AI Automation">
        </div>
        <div class="help-card">
          <h3>Recommended Query Style</h3>
          <div class="hint">- Exact product details: <code>walking stick with seat details</code></div>
          <div class="hint">- Keyword listing: <code>sterile gown listing</code></div>
          <div class="hint">- Quotation: <code>IV Set 50 units quote with 5% discount</code></div>
          <div class="hint">- Template control: <code>bill_to: ABC Hospital; ship_to: Jaipur; bill_to_gstin: 08ABCDE1234F1Z5; terms: 100% advance|Delivery in 7 days</code></div>
        </div>
        <div class="help-card">
          <h3>Response Modes</h3>
          <div class="hint">- Best Pricing Intelligence: lowest vs avg vs highest with delta math</div>
          <div class="hint">- Vendor Coverage: kaun-kaun vendor is product ko cater kar raha hai</div>
          <div class="hint">- Exact Product Inquiry: all fields per offer</div>
          <div class="hint">- Product Inquiry: vendor-wise pricing table</div>
          <div class="hint">- Quotation: GST-ready export details</div>
        </div>
        <div class="help-card">
          <h3>Invoice Modification Prompt</h3>
          <div class="hint">- Step-by-step mode: <code>make a tax invoice of this product</code> or <code>make a purchase invoice of this product</code></div>
          <div class="hint">- Tax Invoice: <code>create tax invoice for IV Set 10 units; bill_to: City Hospital; bill_to_gstin: 08ABCDE1234F1Z5; ship_to: Jaipur</code></div>
          <div class="hint">- Purchase Invoice: <code>create purchase invoice for IV Set 10 units; bill_to: City Hospital; bank_name: HDFC Bank; account_no: 9988776655</code></div>
          <div class="hint">- Field update format (both): <code>field_name: value; field_name: value; terms: 100% advance|Delivery in 7 days; note: urgent dispatch</code></div>
          <div class="hint">- Reset custom fields: <code>reset invoice fields</code></div>
        </div>
        <div class="help-card">
          <h3>Search Controls</h3>
          <div class="control-row">
            <label for="maxProducts">Max Products in Listing</label>
            <input id="maxProducts" type="number" min="1" max="30" value="8">
          </div>
          <div class="control-row">
            <label for="maxOffers">Max Offers per Product</label>
            <input id="maxOffers" type="number" min="1" max="25" value="10">
          </div>
          <div class="check-row">
            <input id="strictKeyword" type="checkbox">
            <label for="strictKeyword">Strict keyword mode (product name token match only)</label>
          </div>
        </div>
      </aside>
    </div>
  </div>

  <script>
    const chat = document.getElementById("chat");
    const landingPage = document.getElementById("landingPage");
    const appShell = document.getElementById("appShell");
    const getStartedBtn = document.getElementById("getStartedBtn");
    const input = document.getElementById("q");
    const btn = document.getElementById("send");
    const typing = document.getElementById("typing");
    const statusDot = document.getElementById("statusDot");
    const statusText = document.getElementById("statusText");
    const exportChatBtn = document.getElementById("exportChatBtn");
    const maxProductsInput = document.getElementById("maxProducts");
    const maxOffersInput = document.getElementById("maxOffers");
    const strictKeywordInput = document.getElementById("strictKeyword");
    const modePrompt = document.getElementById("modePrompt");
    const modeProductLabel = document.getElementById("modeProductLabel");
    const modeSkipBtn = document.getElementById("modeSkipBtn");
    const modeButtons = Array.from(document.querySelectorAll(".mode-btn[data-mode]"));
    const quickButtons = Array.from(document.querySelectorAll(".quick-btn"));
    const API_BASE = window.location.origin && window.location.origin.startsWith("http")
      ? window.location.origin + "__CHAT_PREFIX__"
      : "http://127.0.0.1:5000";
    const transcript = [];
    const MODE_LABELS = {
      best_pricing_math: "Best Pricing Intelligence",
      vendor_coverage: "Vendor Coverage",
      product_details: "Full Product Details",
      vendor_listing: "Vendor Listing",
      best_offer: "Best Offer",
      create_quotation: "Create Quotation",
      create_purchase_invoice: "Create Purchase Invoice",
    };
    let pendingProductForMode = "";
    let invoiceWorkflowActive = false;

    function escapeHtml(text) {
      return String(text || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
    }

    function parseTable(lines) {
      const tableLines = lines.filter((line) => line.includes("|")).map((line) => line.trim());
      if (tableLines.length < 2) return "";
      const headers = tableLines[0].split("|").map((s) => escapeHtml(s.trim()));
      const rows = tableLines.slice(1).map((line) => line.split("|").map((s) => escapeHtml(s.trim())));
      const th = headers.map((h) => `<th>${h}</th>`).join("");
      const tr = rows
        .map((cells) => `<tr>${cells.map((cell) => `<td>${cell}</td>`).join("")}</tr>`)
        .join("");
      return `<table><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table>`;
    }

    function parseKeyValueBlock(lines) {
      const kvRows = [];
      for (const line of lines) {
        const idx = line.indexOf(":");
        if (idx <= 0) continue;
        const key = line.slice(0, idx).trim();
        const value = line.slice(idx + 1).trim();
        if (!key || !value) continue;
        kvRows.push(
          `<div class="k">${escapeHtml(key)}</div><div class="v">${escapeHtml(value)}</div>`
        );
      }
      if (kvRows.length < 3) return "";
      return `<div class="card"><div class="kv">${kvRows.join("")}</div></div>`;
    }

    function parseQuoteSummary(lines) {
      if (!lines.length) return "";
      if (!lines[0].toUpperCase().includes("QUOTATION GENERATED")) return "";
      const kv = parseKeyValueBlock(lines.slice(1));
      const fallback = lines.slice(1).map((line) => `<div class="plain">${escapeHtml(line)}</div>`).join("");
      return `<div class="card"><h4>Quotation Summary</h4>${kv || fallback}</div>`;
    }

    function parseBulletBlock(lines) {
      const bullets = lines
        .filter((line) => line.startsWith("- "))
        .map((line) => `<li>${escapeHtml(line.replace(/^-\\s+/, ""))}</li>`);
      if (bullets.length < 2) return "";
      return `<div class="card"><div class="section-title">Highlights</div><ul class="bullet-list">${bullets.join("")}</ul></div>`;
    }

    function extractExportLinks(rawText) {
      const links = [];
      const regex = /(PDF Export|JSON Export):\\s*([^\\n]+)/gi;
      let match;
      while ((match = regex.exec(rawText)) !== null) {
        const kind = String(match[1] || "").trim().toUpperCase();
        const pathRaw = String(match[2] || "").trim();
        if (!pathRaw) continue;
        const normalized = pathRaw.replace(/\\\\/g, "/");
        const filename = normalized.split("/").pop() || "";
        if (!filename) continue;
        const href = `${API_BASE}/downloads/${encodeURIComponent(filename)}`;
        const label = kind.startsWith("PDF") ? "Download PDF" : "Download JSON";
        links.push(`<a class="download-btn" href="${href}" download="${escapeHtml(filename)}" target="_blank" rel="noopener">${label}</a>`);
      }
      if (!links.length) return "";
      return `<div class="card"><h4>Downloads</h4><div class="download-links">${links.join("")}</div></div>`;
    }

    function formatAgentResponse(text) {
      const raw = String(text || "").trim();
      const blocks = raw.split(/\\n\\s*\\n/).map((b) => b.trim()).filter(Boolean);
      const out = [];
      for (const block of blocks) {
        const lines = block.split("\\n").map((line) => line.trim()).filter(Boolean);
        if (!lines.length) continue;

        const quoteCard = parseQuoteSummary(lines);
        if (quoteCard) {
          out.push(quoteCard);
          continue;
        }

        if (lines[0].startsWith("Intent Understood")) {
          const badges = lines.map((line) => `<span class="badge">${escapeHtml(line)}</span>`).join("");
          out.push(`<div class="card"><h4>Intent Summary</h4>${badges}</div>`);
          continue;
        }

        if (lines[0].toLowerCase().startsWith("by product - vendors and pricing")) {
          out.push(`<div class="card"><h4>Vendor Listing Overview</h4><div class="plain">${escapeHtml(block)}</div></div>`);
          continue;
        }

        if (lines[0].toLowerCase().startsWith("best pricing analysis")) {
          const table = parseTable(lines.slice(1));
          const kv = parseKeyValueBlock(lines.slice(1));
          const body = table || kv || `<div class="plain">${escapeHtml(block)}</div>`;
          out.push(`<div class="card"><h4>Best Pricing Intelligence</h4>${body}</div>`);
          continue;
        }

        if (lines[0].toLowerCase().startsWith("vendor coverage map")) {
          const table = parseTable(lines.slice(1));
          const kv = parseKeyValueBlock(lines.slice(1));
          const body = table || kv || `<div class="plain">${escapeHtml(block)}</div>`;
          out.push(`<div class="card"><h4>Vendor Coverage</h4>${body}</div>`);
          continue;
        }

        if (lines[0].startsWith("Simple Product >")) {
          const title = lines[0].replace("Simple Product >", "").trim();
          const body = lines.slice(1);
          const table = parseTable(body);
          const kv = parseKeyValueBlock(body);
          const rendered = table || kv || `<div class="plain">${escapeHtml(body.join("\\n"))}</div>`;
          out.push(`<div class="card"><h4>${escapeHtml(title || "Product Snapshot")}</h4>${rendered}</div>`);
          continue;
        }

        if (lines.some((line) => line.includes("|")) && lines[0].toLowerCase().includes("s.no")) {
          out.push(`<div class="card"><h4>Vendor Listing</h4>${parseTable(lines)}</div>`);
          continue;
        }

        if (lines[0].startsWith("- Best Offer Detail")) {
          out.push(`<div class="card"><h4>Best Offer</h4><div class="plain">${escapeHtml(block.replace(/^-\\s*/, ""))}</div></div>`);
          continue;
        }

        if (lines[0].startsWith("Offer #")) {
          out.push(`<div class="card"><h4>${escapeHtml(lines[0])}</h4>${parseKeyValueBlock(lines.slice(1)) || `<div class="plain">${escapeHtml(block)}</div>`}</div>`);
          continue;
        }

        const kv = parseKeyValueBlock(lines);
        if (kv) {
          out.push(kv);
          continue;
        }

        const bulletBlock = parseBulletBlock(lines);
        if (bulletBlock) {
          out.push(bulletBlock);
          continue;
        }

        out.push(`<div class="plain">${escapeHtml(block)}</div>`);
      }
      const exportLinks = extractExportLinks(raw);
      if (exportLinks) out.push(exportLinks);
      return out.join("");
    }

    function messageTime() {
      return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    function scrollChatToLatest(force = false) {
      const distanceFromBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight;
      if (force || distanceFromBottom < 140) {
        chat.scrollTo({ top: chat.scrollHeight, behavior: "smooth" });
      }
    }

    function hideModePrompt() {
      pendingProductForMode = "";
      modePrompt.style.display = "none";
      modeProductLabel.textContent = "";
    }

    function showModePrompt(productText) {
      pendingProductForMode = productText;
      modeProductLabel.textContent = productText;
      modePrompt.style.display = "flex";
      scrollChatToLatest(true);
    }

    function looksLikeBareProductName(query) {
      const text = String(query || "").trim();
      if (!text) return false;
      const tokens = text.split(/\\s+/).filter(Boolean);
      // Cap free-text product names; reject multi-sentence pastes.
      if (tokens.length < 2 || tokens.length > 20) return false;
      if (text.length > 180) return false;
      const lower = text.toLowerCase();
      if (/[?]/.test(lower)) return false;
      const conversationalWords = [
        "hi", "hello", "hey", "thanks", "thank", "please", "plz", "help", "issue", "problem",
        "kya", "kaise", "kyu", "nahi", "hai", "ho", "hun", "kar", "karo", "karna", "mujhe",
        "mera", "mere", "aap", "tum", "bhai", "yar", "chat", "normal", "samjho", "samjha",
      ];
      if (conversationalWords.some((word) => lower.includes(word))) return false;
      const intentWords = [
        "quote", "quotation", "invoice", "purchase", "tax", "vendor", "list",
        "listing", "detail", "details", "price", "pricing", "offer", "stock",
        "warranty", "discount", "gst", "po", "compare", "show", "tell", "about",
      ];
      if (intentWords.some((word) => lower.includes(word))) return false;
      return true;
    }

    function looksLikeInvoiceIntent(query) {
      const lower = String(query || "").toLowerCase();
      if (!lower) return false;
      return (
        lower.includes("invoice") ||
        lower.includes("tax invoice") ||
        lower.includes("purchase invoice") ||
        lower.includes("quotation") ||
        lower.includes("quote")
      );
    }

    function buildQueryFromMode(productText, mode) {
      switch (mode) {
        case "best_pricing_math":
          return `${productText} best pricing analysis with mathematical comparison`;
        case "vendor_coverage":
          return `${productText} vendor coverage list with all vendors`;
        case "product_details":
          return `${productText} full details with all fields`;
        case "vendor_listing":
          return `${productText} vendor listing sorted by price`;
        case "best_offer":
          return `${productText} best offer detail`;
        case "create_quotation":
          return `create quotation for ${productText} quantity 1`;
        case "create_purchase_invoice":
          return `create purchase invoice for ${productText} quantity 1`;
        default:
          return productText;
      }
    }

    function addUserMessage(text) {
      transcript.push({ role: "BD Team", text });
      const wrap = document.createElement("div");
      wrap.className = "message user-msg";
      wrap.innerHTML = `
        <div class="avatar user">BD</div>
        <div class="bubble user-bubble">
          ${escapeHtml(text)}
          <div class="meta">${messageTime()}</div>
        </div>
      `;
      chat.appendChild(wrap);
      scrollChatToLatest(true);
    }

    function addBotMessage(text) {
      transcript.push({ role: "Agent", text });
      const wrap = document.createElement("div");
      wrap.className = "message bot-msg";
      const html = formatAgentResponse(text);
      wrap.innerHTML = `
        <div class="avatar bot">AI</div>
        <div class="bubble">
          ${html}
          <div class="bubble-actions">
            <button class="mini-btn">Copy</button>
          </div>
          <div class="meta">${messageTime()}</div>
        </div>
      `;
      const btnCopy = wrap.querySelector(".mini-btn");
      btnCopy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(text);
          btnCopy.textContent = "Copied";
          setTimeout(() => { btnCopy.textContent = "Copy"; }, 1000);
        } catch (e) {
          btnCopy.textContent = "Copy failed";
          setTimeout(() => { btnCopy.textContent = "Copy"; }, 1200);
        }
      });
      chat.appendChild(wrap);
      scrollChatToLatest(true);
      updateInvoiceWorkflowState(text);
    }

    function updateInvoiceWorkflowState(text) {
      const normalized = String(text || "").toLowerCase();
      if (!normalized) return;
        if (
          normalized.includes("invoice workflow started for") ||
          normalized.includes("invoice workflow start ho gaya")
        ) {
        invoiceWorkflowActive = true;
        hideModePrompt();
        return;
      }
        if (normalized.startsWith("noted.") || normalized.startsWith("theek hai.")) {
        invoiceWorkflowActive = true;
        hideModePrompt();
        return;
      }
      if (normalized.startsWith("quotation generated")) {
        invoiceWorkflowActive = false;
        hideModePrompt();
      }
    }

    async function refreshHealth() {
      try {
        const res = await fetch(`${API_BASE}/health`);
        const data = await res.json();
        if (!res.ok) throw new Error("health error");
        statusDot.className = "dot ok";
        statusText.textContent = `Backend Online | Model: ${data.model || "unknown"}`;
      } catch (e) {
        statusDot.className = "dot err";
        statusText.textContent = "Backend Offline";
      }
    }

    async function send(forcedQuery = null, displayText = null) {
      const query = String(forcedQuery ?? input.value).trim();
      if (!query) return;
      // Clear leftover invoice draft when the user starts a product search.
      if (!forcedQuery && invoiceWorkflowActive && !looksLikeInvoiceIntent(query)) {
        invoiceWorkflowActive = false;
      }
      if (!forcedQuery && !invoiceWorkflowActive && looksLikeBareProductName(query)) {
        showModePrompt(query);
        return;
      }
      const options = {
        max_products: Number(maxProductsInput.value || 8),
        max_offers_per_product: Number(maxOffersInput.value || 10),
        strict_keyword: Boolean(strictKeywordInput.checked),
      };
      addUserMessage(displayText || query);
      input.value = "";
      hideModePrompt();
      btn.disabled = true;
      typing.style.display = "block";
      try {
        const res = await fetch(`${API_BASE}/api/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query, options })
        });
        const rawBody = await res.text();
        let data = null;
        if (rawBody) {
          try {
            data = JSON.parse(rawBody);
          } catch (parseErr) {
            data = null;
          }
        }
        if (!res.ok) {
          const errMsg = (data && data.error) ? data.error : `Request failed (${res.status})`;
          throw new Error(errMsg);
        }
        if (!data || typeof data !== "object") {
          throw new Error("Server response was incomplete. Please retry in 5-10 seconds.");
        }
        addBotMessage(data.response || "No response");
      } catch (err) {
        const msg = (err && err.message) ? err.message : "Request failed";
        addBotMessage(`Agent error: ${msg}. Check backend at ${API_BASE}/health`);
      } finally {
        typing.style.display = "none";
        btn.disabled = false;
      }
    }

    btn.addEventListener("click", () => send());
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
    quickButtons.forEach((quickBtn) => {
      quickBtn.addEventListener("click", () => {
        const query = quickBtn.getAttribute("data-q") || "";
        if (!query) return;
        input.value = query;
        send();
      });
    });
    modeButtons.forEach((modeBtn) => {
      modeBtn.addEventListener("click", () => {
        if (!pendingProductForMode) return;
        const mode = modeBtn.getAttribute("data-mode") || "";
        const finalQuery = buildQueryFromMode(pendingProductForMode, mode);
        const modeLabel = MODE_LABELS[mode] || mode;
        send(finalQuery, `${pendingProductForMode} [${modeLabel}]`);
      });
    });
    modeSkipBtn.addEventListener("click", () => {
      if (!pendingProductForMode) {
        hideModePrompt();
        return;
      }
      const plainQuery = pendingProductForMode;
      send(plainQuery, plainQuery);
    });
    exportChatBtn.addEventListener("click", () => {
      const text = transcript.map((item) => `${item.role}: ${item.text}`).join("\\n\\n");
      const blob = new Blob([text || "No chat yet."], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "hospkart-bd-chat.txt";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    });
    getStartedBtn.addEventListener("click", () => {
      landingPage.classList.add("hidden");
      appShell.classList.remove("hidden");
      setTimeout(() => {
        input.focus();
        scrollChatToLatest(true);
      }, 80);
    });
    addBotMessage("QuoteSarthi AI ready hai. Product poochho, vendor listing lo, ya invoice flow start karo.");
    window.addEventListener("load", () => scrollChatToLatest(true));
    refreshHealth();
    setInterval(refreshHealth, 15000);
  </script>
</body>
</html>
"""


@app.get("/health")
def health() -> tuple[dict[str, str | bool], int]:
    return {
        "status": "ok",
        "use_ollama": g.agent.ollama_enabled,
        "model": g.agent.model_name,
    }, 200


@app.after_request
def add_cors_headers(response):  # type: ignore[no-untyped-def]
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.get("/debug/config")
def debug_config() -> tuple[dict[str, object], int]:
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_PORT", "5000"))
    return {
        "flask": {"host": host, "port": port},
        "agent": {
            "use_ollama": g.agent.ollama_enabled,
            "model": g.agent.model_name,
        },
        "rag": {
            "vector_db_path": str(g.agent.rag.db_path),
            "collection": "live-vendor-and-hospkart-catalogue",
            "catalogue": g.agent.rag.source_status(),
        },
        "env": {
            "EMBEDDING_MODEL": os.getenv("EMBEDDING_MODEL"),
            "VECTOR_DB_PATH": os.getenv("VECTOR_DB_PATH"),
            "FLASK_HOST": os.getenv("FLASK_HOST"),
            "FLASK_PORT": os.getenv("FLASK_PORT"),
        },
    }, 200


@app.get("/model-test")
def model_test() -> tuple[dict[str, object], int]:
    prompt = request.args.get(
        "prompt",
        "Generate short BD summary for: Quote for 10 hospital beds with 5% discount.",
    )

    if not g.agent.ollama_enabled:
        return {
            "status": "disabled",
            "message": "USE_OLLAMA is false. Enable it in .env.",
            "model": g.agent.model_name,
        }, 400

    try:
        import ollama

        result = ollama.generate(model=g.agent.model_name, prompt=prompt)
        return {
            "status": "ok",
            "model": g.agent.model_name,
            "prompt": prompt,
            "response": (result or {}).get("response", "").strip(),
        }, 200
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "model": g.agent.model_name,
            "prompt": prompt,
            "error": str(exc),
        }, 500


@app.get("/assets/hospkart-logo")
def hospkart_logo():
    if LOGO_PATH.exists():
        return send_file(LOGO_PATH, mimetype="image/jpeg")
    return {"error": "logo not found"}, 404


@app.get("/assets/hospkart-hero")
def hospkart_hero():
    if HERO_PATH.exists():
        suffix = HERO_PATH.suffix.lower()
        mimetype = "image/png" if suffix == ".png" else "image/jpeg"
        return send_file(HERO_PATH, mimetype=mimetype)
    return {"error": "hero image not found"}, 404


@app.get("/downloads/<path:filename>")
def download_export(filename: str):
    safe_name = Path(filename).name
    if not safe_name:
        return {"error": "invalid filename"}, 400
    exports_root = g.agent.exports_dir.resolve()
    target = (exports_root / safe_name).resolve()
    if not str(target).startswith(str(exports_root)):
        return {"error": "invalid path"}, 400
    if not target.exists() or not target.is_file():
        return {"error": "file not found"}, 404
    return send_file(target, as_attachment=True, download_name=safe_name)


@app.route("/api/chat", methods=["POST", "OPTIONS"])
@app.route("/chat", methods=["POST", "OPTIONS"])
def chat() -> tuple[dict[str, str], int]:
    if request.method == "OPTIONS":
        return {}, 204

    payload = request.get_json(silent=True) or {}
    query = str(payload.get("query") or payload.get("message") or "").strip()
    options = payload.get("options")
    if not isinstance(options, dict):
        options = {}
    if not query:
        return {"error": "query is required (or message)"}, 400

    try:
        with session_agents_lock:
            response = g.agent.handle_query(query, options=options)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}, 500
    return {"query": query, "response": response}, 200


@app.get("/")
def root() -> str:
    return redirect("/vendor/?assistant=1")


@app.get("/ui/")
def embedded_quotesarthi():
    return CHAT_PAGE.replace("__CHAT_PREFIX__", request.script_root.rstrip("/"))


if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_PORT", "5000"))
    app.run(host=host, port=port, debug=False, threaded=True)
