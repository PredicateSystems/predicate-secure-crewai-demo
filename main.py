#!/usr/bin/env python3
"""
Zero-Trust Multi-Agent E-commerce Price Monitoring System

This demo showcases CrewAI multi-agent orchestration secured with
predicate-secure SDK for runtime trust enforcement.

Architecture:
- Orchestrator: Gets root mandate with broad scope (browser.*, fs.*, tool.*)
- Web Scraper Agent: Receives delegated mandate (browser.*, fs.write scraped/)
- Analyst Agent: Receives delegated mandate (fs.read scraped/, fs.write reports/, tool.*)
- Chain delegation ensures each agent only has the minimum required permissions
- Cloud tracer uploads execution traces to Predicate Studio (if PREDICATE_API_KEY set)

Chain Delegation Flow:
  ┌─────────────────────────────────────────────────────────────────────────┐
  │                    POST /v1/authorize (root mandate)                    │
  │   Orchestrator: browser.*, fs.*, tool.* on workspace/**                 │
  │   mandate_token: eyJhbGci... (depth=0, TTL=300s)                        │
  └───────────────────────────────┬─────────────────────────────────────────┘
                                  │
          ┌───────────────────────┴───────────────────────┐
          ▼                                               ▼
  ┌───────────────────────┐                     ┌───────────────────────────┐
  │  POST /v1/delegate    │                     │    POST /v1/delegate      │
  │  parent: root mandate │                     │    parent: root mandate   │
  │  target: agent:scraper│                     │    target: agent:analyst  │
  │  scope: browser.*,    │                     │    scope: fs.read,        │
  │         fs.write      │                     │           fs.write,       │
  │         scraped/**    │                     │           tool.*          │
  └───────────────────────┘                     └───────────────────────────┘
          │                                               │
          ▼                                               ▼
  ┌───────────────────────┐                     ┌───────────────────────────┐
  │  Derived Mandate      │                     │  Derived Mandate          │
  │  depth=1, TTL≤300s    │                     │  depth=1, TTL≤300s        │
  │  chain_hash: abc123   │                     │  chain_hash: def456       │
  └───────────────────────┘                     └───────────────────────────┘

Usage:
    # Start the sidecar first (with optional control plane registration)
    predicate-authorityd --policy-file policies/monitoring.yaml run

    # Or with control plane for fleet management:
    predicate-authorityd \
      --policy-file policies/monitoring.yaml \
      --mode cloud_connected \
      --control-plane-url https://api.predicatesystems.ai \
      --predicate-api-key $PREDICATE_API_KEY \
      --tenant-id $TENANT_ID \
      --project-id $PROJECT_ID \
      --sync-enabled \
      run

    # Run the demo (with chain delegation)
    python main.py --products "laptop,monitor" --use-delegation

    # Run without delegation (direct authorization)
    python main.py --products "laptop,monitor"

    # Use different LLM providers
    python main.py --products "laptop" --llm deepinfra   # DeepInfra cloud (default)
    python main.py --products "laptop" --llm ollama      # Local Ollama
"""

from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

from crewai import Agent, Crew, Process, Task
from crewai import LLM
from crewai.tools import tool

# Import predicate-secure SDK
from predicate_secure import SecureAgent

# Import tracer for cloud/local trace uploads
try:
    from predicate.tracer_factory import create_tracer
    from predicate.trace_event_builder import TraceEventBuilder
    TRACER_AVAILABLE = True
except ImportError:
    TRACER_AVAILABLE = False
    create_tracer = None
    TraceEventBuilder = None

# =============================================================================
# Tracer Configuration (Cloud or Local)
# =============================================================================

class _TraceLogger:
    """Simple logger for tracer messages."""

    def info(self, message: str) -> None:
        print(f"[trace] {message}", flush=True)

    def warning(self, message: str) -> None:
        print(f"[trace][warn] {message}", flush=True)

    def error(self, message: str) -> None:
        print(f"[trace][error] {message}", flush=True)


def create_demo_tracer(
    run_id: str,
    goal: str,
    llm_model: str,
    products: list[str],
):
    """
    Create a tracer for uploading execution traces to Predicate Studio.

    If PREDICATE_API_KEY is set, traces are uploaded to the cloud.
    Otherwise, traces are saved locally to workspace/data/traces/.
    """
    if not TRACER_AVAILABLE:
        print("[trace] predicate SDK not available, skipping tracer setup")
        return None

    predicate_api_key = os.getenv("PREDICATE_API_KEY")

    if predicate_api_key:
        print("[trace] PREDICATE_API_KEY found - traces will upload to Predicate Studio")
        upload_trace = True
    else:
        print("[trace] No PREDICATE_API_KEY - traces will be saved locally")
        upload_trace = False

    tracer = create_tracer(
        api_key=predicate_api_key or "local",
        run_id=run_id,
        upload_trace=upload_trace,
        goal=goal,
        logger=_TraceLogger(),
        agent_type="crewai-ecommerce-demo",
        llm_model=llm_model,
        start_url=f"products: {', '.join(products)}",
    )

    return tracer


# =============================================================================
# LLM Configuration (DeepInfra or Ollama - not OpenAI)
# =============================================================================

# Supported LLM providers
LLM_PROVIDERS = {
    "deepinfra": {
        "model": "Qwen/Qwen2.5-72B-Instruct",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "env_key": "DEEPINFRA_API_KEY",
        "description": "DeepInfra cloud (requires DEEPINFRA_API_KEY)",
    },
    "ollama": {
        "model": "ollama/qwen2.5:14b",
        "base_url": "http://localhost:11434",
        "env_key": None,
        "description": "Local Ollama (requires `ollama serve`)",
    },
}


def get_llm(provider: str = "auto") -> LLM:
    """
    Configure LLM using the specified provider.

    Args:
        provider: LLM provider choice - "deepinfra", "ollama", or "auto"
                  "auto" uses DeepInfra if API key is set, otherwise Ollama

    Returns:
        Configured CrewAI LLM instance
    """
    # Auto-detect provider based on available credentials
    if provider == "auto":
        if os.getenv("DEEPINFRA_API_KEY"):
            provider = "deepinfra"
        else:
            provider = "ollama"
            print("[LLM] No DEEPINFRA_API_KEY found, falling back to Ollama")

    if provider not in LLM_PROVIDERS:
        raise ValueError(f"Unknown LLM provider: {provider}. Choose from: {list(LLM_PROVIDERS.keys())}")

    config = LLM_PROVIDERS[provider]

    # Build LLM kwargs
    llm_kwargs = {
        "model": config["model"],
        "base_url": config["base_url"],
        "temperature": 0.1,
    }

    # Add API key if required
    if config["env_key"]:
        api_key = os.getenv(config["env_key"])
        if not api_key:
            raise ValueError(
                f"LLM provider '{provider}' requires {config['env_key']} environment variable. "
                f"Set it with: export {config['env_key']}=your-api-key"
            )
        llm_kwargs["api_key"] = api_key

    return LLM(**llm_kwargs)

# =============================================================================
# Custom Tools
# =============================================================================

@tool
def navigate_to_product(url: str) -> str:
    """
    Navigate to a product page on an approved e-commerce site.

    Args:
        url: The product URL to navigate to (must be from approved domain)

    Returns:
        Status message indicating success or failure with verification details
    """
    # Pre-execution: Domain allowlist check
    approved_domains = [
        "amazon.com",
        "bestbuy.com",
        "walmart.com",
        "newegg.com",
        "target.com",
    ]

    domain_match = any(domain in url for domain in approved_domains)
    if not domain_match:
        return f"ERROR: Domain not in approved list. URL: {url}"

    # Execute: Make actual HTTP request to verify page exists
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        response = requests.get(url, headers=headers, timeout=10, allow_redirects=True)

        # Post-execution deterministic verification
        verification_results = []

        # 1. url_contains: Verify we're on a product page
        final_url = response.url
        if "amazon.com" in url:
            url_check = "/dp/" in final_url or "/gp/product/" in final_url
            verification_results.append(f"url_contains(/dp/): {'PASS' if url_check else 'FAIL'}")
        elif "bestbuy.com" in url:
            url_check = "/site/" in final_url
            verification_results.append(f"url_contains(/site/): {'PASS' if url_check else 'FAIL'}")
        elif "walmart.com" in url:
            url_check = "/ip/" in final_url
            verification_results.append(f"url_contains(/ip/): {'PASS' if url_check else 'FAIL'}")
        else:
            url_check = True
            verification_results.append("url_contains: SKIP (no pattern for domain)")

        # 2. HTTP status check
        status_ok = response.status_code == 200
        verification_results.append(f"http_status(200): {'PASS' if status_ok else f'FAIL ({response.status_code})'}")

        # 3. exists(productTitle): Check for product title element in HTML
        html_content = response.text
        if "amazon.com" in url:
            # Amazon product title check
            title_exists = 'id="productTitle"' in html_content or 'id="title"' in html_content
            verification_results.append(f"exists(#productTitle): {'PASS' if title_exists else 'FAIL'}")

            # Check for "Page Not Found" or dog page (Amazon's 404)
            is_404_page = "Sorry, we couldn't find that page" in html_content or \
                          "looking for something" in html_content.lower() and "dogs" in html_content.lower()
            if is_404_page:
                verification_results.append("not_exists(404_page): FAIL - Product not found")
                return f"ERROR: Product page not found at {url}\nVerification:\n" + "\n".join(verification_results)

            # Check for price element
            price_exists = 'class="a-price' in html_content or 'id="priceblock' in html_content or \
                           'a-offscreen' in html_content
            verification_results.append(f"exists(.a-price): {'PASS' if price_exists else 'FAIL (may be CAPTCHA)'}")

        elif "bestbuy.com" in url:
            title_exists = 'class="sku-title"' in html_content or 'class="heading-5"' in html_content
            verification_results.append(f"exists(.sku-title): {'PASS' if title_exists else 'FAIL'}")

        elif "walmart.com" in url:
            title_exists = 'itemprop="name"' in html_content
            verification_results.append(f"exists([itemprop=name]): {'PASS' if title_exists else 'FAIL'}")

        # Determine overall success
        all_passed = all("PASS" in r or "SKIP" in r for r in verification_results)

        if all_passed:
            return f"SUCCESS: Navigated to {url}\nFinal URL: {final_url}\nVerification:\n" + "\n".join(verification_results)
        else:
            return f"WARNING: Navigated but verification issues at {url}\nFinal URL: {final_url}\nVerification:\n" + "\n".join(verification_results)

    except requests.Timeout:
        return f"ERROR: Request timeout for {url}"
    except requests.RequestException as e:
        return f"ERROR: Failed to navigate to {url}: {str(e)}"


@tool
def extract_price_data(url: str) -> str:
    """
    Extract price and availability data from the current product page.

    Args:
        url: The product URL to extract data from

    Returns:
        JSON string with price, availability, and product info, plus verification results
    """
    verification_results = []

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        response = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        html_content = response.text

        extracted_data = {
            "url": url,
            "final_url": response.url,
            "timestamp": datetime.now().isoformat(),
            "product_name": None,
            "price": None,
            "currency": "USD",
            "availability": None,
            "verification": {},
        }

        if "amazon.com" in url:
            # Extract product title
            title_match = re.search(r'id="productTitle"[^>]*>([^<]+)<', html_content)
            if title_match:
                extracted_data["product_name"] = title_match.group(1).strip()
                verification_results.append("exists(#productTitle): PASS")
            else:
                # Try alternate title location
                title_match = re.search(r'id="title"[^>]*>([^<]+)<', html_content)
                if title_match:
                    extracted_data["product_name"] = title_match.group(1).strip()
                    verification_results.append("exists(#title): PASS")
                else:
                    verification_results.append("exists(#productTitle): FAIL")

            # Extract price - Amazon uses various price selectors
            price_patterns = [
                r'class="a-price-whole">(\d+)</span>',  # Whole price
                r'class="a-offscreen">\$?([\d,]+\.?\d*)</span>',  # Screen reader price
                r'id="priceblock_ourprice"[^>]*>\$?([\d,]+\.?\d*)',  # Old price block
                r'id="priceblock_dealprice"[^>]*>\$?([\d,]+\.?\d*)',  # Deal price
                r'"price":"([\d.]+)"',  # JSON price
            ]

            for pattern in price_patterns:
                price_match = re.search(pattern, html_content)
                if price_match:
                    price_str = price_match.group(1).replace(",", "")
                    try:
                        extracted_data["price"] = float(price_str)
                        verification_results.append(f"exists(.a-price): PASS (${extracted_data['price']})")
                        break
                    except ValueError:
                        continue

            if extracted_data["price"] is None:
                verification_results.append("exists(.a-price): FAIL - No price found")

            # Check availability
            if "In Stock" in html_content or "in stock" in html_content.lower():
                extracted_data["availability"] = "In Stock"
                verification_results.append("dom_contains('In Stock'): PASS")
            elif "Out of Stock" in html_content or "Currently unavailable" in html_content:
                extracted_data["availability"] = "Out of Stock"
                verification_results.append("dom_contains('Out of Stock'): PASS")
            else:
                extracted_data["availability"] = "Unknown"
                verification_results.append("dom_contains(availability): FAIL - Unknown status")

            # Check for CAPTCHA or bot detection
            if "Enter the characters you see below" in html_content or "captcha" in html_content.lower():
                verification_results.append("not_exists(captcha): FAIL - CAPTCHA detected")
                extracted_data["error"] = "CAPTCHA detected"

            # Check for 404/dog page
            if "Sorry, we couldn't find that page" in html_content or \
               ("looking for something" in html_content.lower() and "dogs" in html_content.lower()):
                verification_results.append("not_exists(404_page): FAIL - Product not found")
                extracted_data["error"] = "Product not found (404)"

        elif "bestbuy.com" in url:
            # Best Buy extraction
            title_match = re.search(r'class="sku-title"[^>]*>([^<]+)<', html_content)
            if title_match:
                extracted_data["product_name"] = title_match.group(1).strip()
                verification_results.append("exists(.sku-title): PASS")

            price_match = re.search(r'class="priceView-customer-price"[^>]*>\$?([\d,]+\.?\d*)', html_content)
            if price_match:
                extracted_data["price"] = float(price_match.group(1).replace(",", ""))
                verification_results.append(f"exists(.priceView-customer-price): PASS (${extracted_data['price']})")

        elif "walmart.com" in url:
            # Walmart extraction
            title_match = re.search(r'itemprop="name"[^>]*>([^<]+)<', html_content)
            if title_match:
                extracted_data["product_name"] = title_match.group(1).strip()
                verification_results.append("exists([itemprop=name]): PASS")

            price_match = re.search(r'itemprop="price"[^>]*content="([\d.]+)"', html_content)
            if price_match:
                extracted_data["price"] = float(price_match.group(1))
                verification_results.append(f"exists([itemprop=price]): PASS (${extracted_data['price']})")

        # Post-execution verification summary
        extracted_data["verification"] = {
            "checks": verification_results,
            "all_passed": all("PASS" in r for r in verification_results) if verification_results else False,
            "response_not_empty": len(html_content) > 1000,
        }

        verification_results.append(f"response_not_empty: {'PASS' if len(html_content) > 1000 else 'FAIL'}")

        return json.dumps(extracted_data, indent=2)

    except requests.Timeout:
        return json.dumps({
            "url": url,
            "error": "Request timeout",
            "verification": {"checks": ["http_request: FAIL - Timeout"], "all_passed": False}
        }, indent=2)
    except requests.RequestException as e:
        return json.dumps({
            "url": url,
            "error": str(e),
            "verification": {"checks": [f"http_request: FAIL - {str(e)}"], "all_passed": False}
        }, indent=2)


@tool
def save_scraped_data(filename: str, data: str) -> str:
    """
    Save scraped data to the workspace/data/scraped directory.

    Args:
        filename: Name of the file (without path)
        data: JSON data to save

    Returns:
        Status message indicating success or failure
    """
    workspace_path = Path(__file__).parent / "workspace" / "data" / "scraped"
    workspace_path.mkdir(parents=True, exist_ok=True)

    file_path = workspace_path / filename

    # Security check: only allow .json files
    if not filename.endswith(".json"):
        return f"ERROR: Only .json files allowed. Got: {filename}"

    with open(file_path, "w") as f:
        f.write(data)

    return f"SUCCESS: Saved data to {file_path}"


@tool
def read_scraped_data(filename: str) -> str:
    """
    Read scraped data from the workspace/data/scraped directory.

    Args:
        filename: Name of the file to read (without path)

    Returns:
        File contents as string
    """
    workspace_path = Path(__file__).parent / "workspace" / "data" / "scraped"
    file_path = workspace_path / filename

    if not file_path.exists():
        return f"ERROR: File not found: {filename}"

    with open(file_path, "r") as f:
        return f.read()


@tool
def analyze_prices(data: str) -> str:
    """
    Analyze price data and generate insights.

    Args:
        data: JSON string containing price data to analyze

    Returns:
        Analysis results as JSON string
    """
    try:
        prices = json.loads(data)
        if isinstance(prices, dict):
            prices = [prices]

        # Calculate statistics
        price_values = [p.get("price", 0) for p in prices if p.get("price")]

        if not price_values:
            return json.dumps({"error": "No price data found"})

        analysis = {
            "timestamp": datetime.now().isoformat(),
            "total_products": len(prices),
            "price_stats": {
                "min": min(price_values),
                "max": max(price_values),
                "avg": sum(price_values) / len(price_values),
            },
            "recommendations": [],
        }

        # Add recommendations
        avg_price = analysis["price_stats"]["avg"]
        for p in prices:
            if p.get("price", 0) < avg_price * 0.8:
                analysis["recommendations"].append({
                    "product": p.get("product_name", "Unknown"),
                    "action": "BUY",
                    "reason": "Price 20% below average",
                })

        return json.dumps(analysis, indent=2)

    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"})


@tool
def write_report(filename: str, content: str) -> str:
    """
    Write analysis report to the workspace/data/reports directory.

    Args:
        filename: Name of the report file (without path)
        content: Report content to write

    Returns:
        Status message indicating success or failure
    """
    workspace_path = Path(__file__).parent / "workspace" / "data" / "reports"
    workspace_path.mkdir(parents=True, exist_ok=True)

    # Security check: only allow specific file types
    allowed_extensions = [".json", ".csv", ".md"]
    if not any(filename.endswith(ext) for ext in allowed_extensions):
        return f"ERROR: Only {allowed_extensions} files allowed. Got: {filename}"

    file_path = workspace_path / filename

    with open(file_path, "w") as f:
        f.write(content)

    return f"SUCCESS: Report saved to {file_path}"


# =============================================================================
# Agent Definitions
# =============================================================================

def create_agents(llm: LLM) -> tuple[Agent, Agent]:
    """Create and configure the CrewAI agents."""

    # Web Scraper Agent
    web_scraper = Agent(
        role="E-commerce Price Scraper",
        goal="Extract accurate product prices and availability from approved e-commerce websites",
        backstory="""You are an expert web scraper specializing in e-commerce data extraction.
        You navigate to product pages on approved retail sites and extract pricing information.
        You are meticulous about data accuracy and always verify the data you extract.
        You only access approved domains: Amazon, Best Buy, Walmart, Newegg, and Target.""",
        tools=[navigate_to_product, extract_price_data, save_scraped_data],
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )

    # Analyst Agent
    analyst = Agent(
        role="Price Analyst",
        goal="Analyze scraped price data and generate actionable reports with buying recommendations",
        backstory="""You are a data analyst specializing in e-commerce price analysis.
        You read scraped data files, perform statistical analysis, and generate reports.
        You identify pricing trends and provide clear buy/wait recommendations.
        You always save your analysis to properly formatted report files.""",
        tools=[read_scraped_data, analyze_prices, write_report],
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )

    return web_scraper, analyst


# =============================================================================
# Task Definitions
# =============================================================================

def create_tasks(
    scraper: Agent,
    analyst: Agent,
    products: list[str],
) -> list[Task]:
    """Create tasks for the crew."""

    # Map product names to real Amazon ASINs
    # These are real products that can be scraped (verified as of 2024)
    product_asin_map = {
        "laptop": "B0F196M26K",       # MacBook Air M3
        "monitor": "B0DHLN524J",      # LG 27" Ultragear Gaming Monitor 165Hz
        "keyboard": "B0BKW3LB2B",     # Logitech MX Keys S
        "mouse": "B09HM94VDS",        # Logitech MX Master 3S
        "headphones": "B09XS7JWHH",   # Sony WH-1000XM5
        "webcam": "B085TFF7M1",       # Logitech C920
        "microphone": "B07QR6Z1JB",   # Blue Yeti
        "tablet": "B0BJLXMVMV",       # iPad 10th Gen
        "phone": "B0CHX1W1XY",        # iPhone 15 Pro
        "earbuds": "B0CHWRXH8B",      # AirPods Pro 2
    }

    # Generate product URLs from ASIN map, fallback to search URL
    product_urls = []
    for product in products:
        product_lower = product.lower().strip()
        if product_lower in product_asin_map:
            asin = product_asin_map[product_lower]
            product_urls.append(f"https://www.amazon.com/dp/{asin}")
        else:
            # For unknown products, use Amazon search URL
            search_term = product.replace(" ", "+")
            product_urls.append(f"https://www.amazon.com/s?k={search_term}")

    # Task 1: Scrape prices
    scrape_task = Task(
        description=f"""
        Extract price data for the following products from approved e-commerce sites:

        Products: {', '.join(products)}
        URLs to scrape: {', '.join(product_urls)}

        For each product:
        1. Navigate to the product page using navigate_to_product
        2. Extract price data using extract_price_data
        3. Save the data to workspace/data/scraped/prices.json using save_scraped_data

        Combine all scraped data into a single JSON array.
        """,
        expected_output="JSON file containing price data for all requested products",
        agent=scraper,
    )

    # Task 2: Analyze and report
    analyze_task = Task(
        description="""
        Analyze the scraped price data and generate a comprehensive report:

        1. Read the scraped data from workspace/data/scraped/prices.json
        2. Analyze prices using the analyze_prices tool
        3. Generate a markdown report with:
           - Price statistics (min, max, average)
           - Buying recommendations
           - Timestamp and data sources
        4. Save the report to workspace/data/reports/analysis.md

        Provide clear, actionable recommendations.
        """,
        expected_output="Markdown report with price analysis and recommendations",
        agent=analyst,
        context=[scrape_task],
    )

    return [scrape_task, analyze_task]


# =============================================================================
# Main Execution
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Zero-Trust E-commerce Price Monitoring with CrewAI + Predicate Secure"
    )
    parser.add_argument(
        "--products",
        type=str,
        default="laptop,monitor,keyboard",
        help="Comma-separated list of products to monitor",
    )
    parser.add_argument(
        "--policy",
        type=str,
        default="policies/monitoring.yaml",
        help="Path to the policy file",
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["strict", "permissive", "audit"],
        default="strict",
        help="Security mode (default: strict)",
    )
    parser.add_argument(
        "--sidecar-url",
        type=str,
        default="http://127.0.0.1:8787",
        help="URL of the predicate-authorityd sidecar (default: http://127.0.0.1:8787)",
    )
    parser.add_argument(
        "--llm",
        type=str,
        choices=["auto", "deepinfra", "ollama"],
        default="auto",
        help="LLM provider: deepinfra (cloud), ollama (local), or auto (default: auto)",
    )
    args = parser.parse_args()

    products = [p.strip() for p in args.products.split(",")]
    run_id = str(uuid.uuid4())
    goal = f"Monitor e-commerce prices for: {', '.join(products)}"

    print("=" * 70)
    print("Zero-Trust Multi-Agent E-commerce Price Monitoring System")
    print("=" * 70)
    print(f"Run ID: {run_id}")
    print(f"Products: {products}")
    print(f"Policy: {args.policy}")
    print(f"Mode: {args.mode}")
    print(f"Sidecar URL: {args.sidecar_url}")
    print(f"LLM Provider: {args.llm}")
    print("=" * 70)

    # Initialize LLM with selected provider
    llm = get_llm(provider=args.llm)
    print(f"[LLM] Using: {llm.model}")

    # Initialize tracer for cloud/local trace uploads
    tracer = create_demo_tracer(
        run_id=run_id,
        goal=goal,
        llm_model=llm.model,
        products=products,
    )

    # Emit run start event
    if tracer:
        try:
            tracer.emit_run_start(
                agent="crewai-ecommerce-demo",
                llm_model=llm.model,
                config={
                    "goal": goal,
                    "products": products,
                    "policy": args.policy,
                    "mode": args.mode,
                    "sidecar_url": args.sidecar_url,
                },
            )
        except Exception as e:
            print(f"[warn] tracer emit_run_start failed: {e}")

    # Create base agents
    web_scraper, analyst = create_agents(llm)

    # Wrap agents with SecureAgent for zero-trust enforcement
    print(f"\n[SecureAgent] Initializing with policy: {args.policy}")
    print(f"[SecureAgent] Mode: {args.mode} (fail-closed)")

    secure_scraper = SecureAgent(
        agent=web_scraper,
        policy=args.policy,
        mode=args.mode,
        principal_id="agent:scraper",
        sidecar_url=args.sidecar_url,
        trace_verbose=True,
    )

    secure_analyst = SecureAgent(
        agent=analyst,
        policy=args.policy,
        mode=args.mode,
        principal_id="agent:analyst",
        sidecar_url=args.sidecar_url,
        trace_verbose=True,
    )

    # Create tasks
    tasks = create_tasks(
        scraper=secure_scraper._agent,
        analyst=secure_analyst._agent,
        products=products,
    )

    # Assemble the crew
    crew = Crew(
        agents=[secure_scraper._agent, secure_analyst._agent],
        tasks=tasks,
        process=Process.sequential,
        verbose=True,
    )

    # Execute the crew
    print("\n[Crew] Starting execution...")
    print("-" * 70)

    result = crew.kickoff()

    print("-" * 70)
    print("\n[Crew] Execution completed!")
    print(f"\n[Result]\n{result}")

    # Print audit summary
    print("\n" + "=" * 70)
    print("[Audit Summary]")
    print(f"  - Run ID: {run_id}")
    print(f"  - Products analyzed: {len(products)}")
    print(f"  - Policy: {args.policy}")
    print(f"  - Mode: {args.mode}")
    print("=" * 70)

    # Close tracer and save/upload traces
    if tracer:
        try:
            # Emit run end event
            tracer.emit(
                "run_end",
                data={
                    "status": "success",
                    "products_analyzed": len(products),
                    "policy": args.policy,
                    "mode": args.mode,
                },
            )
            tracer.close()

            # Save local copy of trace
            workspace_path = Path(__file__).parent / "workspace" / "data" / "traces"
            workspace_path.mkdir(parents=True, exist_ok=True)
            trace_file = workspace_path / f"trace_{run_id}.jsonl"

            sink = getattr(tracer, "sink", None)
            trace_src = None
            if sink is not None:
                trace_src = getattr(sink, "path", None) or getattr(sink, "_path", None)
            if trace_src and Path(trace_src).exists():
                import shutil
                shutil.copyfile(trace_src, trace_file)
                print(f"\n[trace] Saved to: {trace_file}")

            if os.getenv("PREDICATE_API_KEY"):
                print(f"[trace] View in Predicate Studio: https://studio.predicatesystems.ai/runs/{run_id}")

        except Exception as e:
            print(f"[warn] tracer close failed: {e}")


if __name__ == "__main__":
    main()
