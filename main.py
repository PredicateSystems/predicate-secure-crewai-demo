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
import uuid
from datetime import datetime
from pathlib import Path

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
        Status message indicating success or failure
    """
    # In production, this would use Playwright/browser-use
    # For demo, we simulate the navigation
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

    # Simulate navigation
    return f"SUCCESS: Navigated to {url}"


@tool
def extract_price_data(url: str) -> str:
    """
    Extract price and availability data from the current product page.

    Args:
        url: The product URL to extract data from

    Returns:
        JSON string with price, availability, and product info
    """
    # Simulated price extraction (in production, use browser automation)
    simulated_data = {
        "url": url,
        "timestamp": datetime.now().isoformat(),
        "product_name": "Sample Product",
        "price": 299.99,
        "currency": "USD",
        "availability": "In Stock",
        "seller": "Example Seller",
    }

    return json.dumps(simulated_data, indent=2)


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

    # Generate product URLs (simulated)
    product_urls = [
        f"https://www.amazon.com/dp/B0{i:06d}"
        for i, _ in enumerate(products)
    ]

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
        scraper=secure_scraper.agent,
        analyst=secure_analyst.agent,
        products=products,
    )

    # Assemble the crew
    crew = Crew(
        agents=[secure_scraper.agent, secure_analyst.agent],
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
    print(f"  - Scraper actions: {secure_scraper.action_count}")
    print(f"  - Analyst actions: {secure_analyst.action_count}")
    print(f"  - Total allowed: {secure_scraper.allowed_count + secure_analyst.allowed_count}")
    print(f"  - Total denied: {secure_scraper.denied_count + secure_analyst.denied_count}")
    print("=" * 70)

    # Close tracer and save/upload traces
    if tracer:
        try:
            # Emit run end event
            tracer.emit(
                "run_end",
                data={
                    "status": "success",
                    "scraper_actions": secure_scraper.action_count,
                    "analyst_actions": secure_analyst.action_count,
                    "total_allowed": secure_scraper.allowed_count + secure_analyst.allowed_count,
                    "total_denied": secure_scraper.denied_count + secure_analyst.denied_count,
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
