#!/usr/bin/env python3
"""
Zero-Trust Multi-Agent E-commerce Price Monitoring System

This demo showcases CrewAI multi-agent orchestration secured with
predicate-secure SDK for runtime trust enforcement.

Architecture:
- Orchestrator: Gets MULTI-SCOPE root mandate covering browser.* + fs.* in ONE mandate
- Web Scraper Agent: Receives delegated mandate (browser.* scope from parent)
- Analyst Agent: Receives delegated mandate (fs.* scope from same parent)
- Multi-scope mandate enables:
  • Unified audit trail (single mandate = single audit entry)
  • Cascade revocation (revoking orchestrator mandate revokes all children)
  • Simpler code (one mandate to track instead of N separate mandates)
- Cloud tracer uploads execution traces to Predicate Studio (if PREDICATE_API_KEY set)

Chain Delegation Flow (Multi-Scope):
  ┌─────────────────────────────────────────────────────────────────────────┐
  │           POST /v1/authorize (MULTI-SCOPE root mandate)                 │
  │   Orchestrator scopes: [{browser.*, https://...}, {fs.*, workspace}]    │
  │   mandate_token: eyJhbGci... (depth=0, TTL=300s)                        │
  │   scopes_authorized: [{action: browser.*, ...}, {action: fs.*, ...}]    │
  └───────────────────────────────┬─────────────────────────────────────────┘
                                  │
          ┌───────────────────────┴───────────────────────┐
          ▼                                               ▼
  ┌───────────────────────┐                     ┌───────────────────────────┐
  │  POST /v1/delegate    │                     │    POST /v1/delegate      │
  │  parent: SAME mandate │                     │    parent: SAME mandate   │
  │  target: agent:scraper│                     │    target: agent:analyst  │
  │  scope: browser.*     │                     │    scope: fs.*            │
  │         https://...   │                     │           workspace/...   │
  │  (matches browser.*)  │                     │    (matches fs.*)         │
  └───────────────────────┘                     └───────────────────────────┘
          │                                               │
          ▼                                               ▼
  ┌───────────────────────┐                     ┌───────────────────────────┐
  │  Derived Mandate      │                     │  Derived Mandate          │
  │  depth=1, TTL≤300s    │                     │  depth=1, TTL≤300s        │
  │  chain_hash: abc123   │                     │  chain_hash: def456       │
  └───────────────────────┘                     └───────────────────────────┘

Key difference: Both child delegations use the SAME parent mandate token.
Child scope is validated against ALL parent scopes (OR semantics).

Usage:
    # Start the sidecar first (with optional control plane registration)
    predicate-authorityd --policy-file policies/monitoring.yaml run

    # Or with control plane for fleet management:
    predicate-authorityd \
      --policy-file policies/monitoring.yaml \
      --mode cloud_connected \
      --control-plane-url https://api.predicatesystems.dev \
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
import asyncio
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

# Import Predicate SDK for browser automation and snapshots
try:
    from predicate import (
        PredicateBrowser,  # Sync browser for CrewAI tool compatibility
        PredicateDebugger,
        url_contains,
        exists,
        find,  # Query function for finding elements in snapshots
        snapshot,  # Sync snapshot function
    )
    from predicate.models import ScreenshotConfig, SnapshotOptions
    from predicate.tracer_factory import create_tracer
    from predicate.trace_event_builder import TraceEventBuilder
    from predicate.llm_interaction_handler import LLMInteractionHandler
    PREDICATE_SDK_AVAILABLE = True
    TRACER_AVAILABLE = True
except ImportError:
    PREDICATE_SDK_AVAILABLE = False
    TRACER_AVAILABLE = False
    PredicateBrowser = None
    PredicateDebugger = None
    ScreenshotConfig = None
    SnapshotOptions = None
    create_tracer = None
    TraceEventBuilder = None
    LLMInteractionHandler = None
    url_contains = None
    exists = None
    find = None
    snapshot = None

# =============================================================================
# Browser Configuration
# =============================================================================

# Browser settings for Playwright-based scraping
HEADLESS = True  # Run browser in headless mode (no GUI)
SCREENSHOT_FORMAT = "jpeg"  # Screenshot format: "jpeg" or "png"
SCREENSHOT_QUALITY = 60  # JPEG quality (1-100)
BROWSER_TIMEOUT_MS = 30000  # Page load timeout in milliseconds

# Global browser instance (initialized in main)
# Using Optional for Python 3.9 compatibility
from typing import Optional, Any, List, Dict
from dataclasses import dataclass, field

_browser_instance: Optional[Any] = None  # AsyncPredicateBrowser
_debugger_instance: Optional[Any] = None  # PredicateDebugger
_page_instance: Optional[Any] = None  # Playwright Page
_tracer_instance: Optional[Any] = None  # Tracer for emitting step data

# =============================================================================
# Chain Delegation Client
# =============================================================================

@dataclass
class DelegateResponse:
    """Response from POST /v1/delegate or /v1/authorize endpoint."""
    mandate_token: str
    mandate_id: str
    expires_at: int
    delegation_depth: int
    delegation_chain_hash: str
    # For multi-scope mandates
    scopes_authorized: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class DelegationClient:
    """
    HTTP client for chain delegation via the predicate-authorityd sidecar.

    Implements the delegation flow from the architecture diagram:
      Orchestrator (root mandate) → POST /v1/delegate → Derived mandates for agents

    Example:
        >>> client = DelegationClient("http://127.0.0.1:8787")
        >>> # Get root mandate for orchestrator
        >>> root = await client.authorize_root("agent:orchestrator", "browser.*", "workspace/**")
        >>> # Delegate narrower scope to scraper
        >>> scraper_mandate = await client.delegate(
        ...     parent_mandate_token=root.mandate_token,
        ...     target_agent_id="agent:scraper",
        ...     requested_action="browser.navigate",
        ...     requested_resource="https://www.amazon.com/*",
        ... )
    """
    base_url: str = "http://127.0.0.1:8787"
    timeout_s: float = 5.0

    async def authorize_root(
        self,
        principal: str,
        action: str,
        resource: str,
        intent_hash: Optional[str] = None,
    ) -> DelegateResponse:
        """
        Get root mandate (depth=0) for the orchestrator (single-scope).

        Args:
            principal: The orchestrator principal ID (e.g., "agent:orchestrator")
            action: Broad action scope (e.g., "browser.*" or "*")
            resource: Broad resource scope (e.g., "workspace/**" or "*")
            intent_hash: Optional intent hash

        Returns:
            DelegateResponse with root mandate token
        """
        import httpx

        request_body = {
            "principal": principal,
            "action": action,
            "resource": resource,
            "intent_hash": intent_hash or f"root:{principal}:{action}:{resource}",
            "labels": [],
        }

        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_s) as client:
            response = await client.post("/v1/authorize", json=request_body)

            if response.status_code == 403:
                data = response.json()
                raise RuntimeError(f"Root authorization denied: {data.get('reason', 'unknown')}")

            if not response.is_success:
                raise RuntimeError(f"Root authorization failed: {response.status_code} - {response.text}")

            data = response.json()

            if not data.get("allowed", False):
                raise RuntimeError(f"Root authorization denied: {data.get('reason', 'unknown')}")

            # Note: /v1/authorize returns mandate_id, but for delegation we need
            # the full mandate_token. In the real sidecar, authorize returns the token.
            return DelegateResponse(
                mandate_token=data.get("mandate_token", data.get("mandate_id", "")),
                mandate_id=data.get("mandate_id", ""),
                expires_at=data.get("expires_at", 0),
                delegation_depth=0,
                delegation_chain_hash=data.get("delegation_chain_hash", "root"),
            )

    async def authorize_root_multi_scope(
        self,
        principal: str,
        scopes: List[Dict[str, str]],
        intent_hash: Optional[str] = None,
    ) -> DelegateResponse:
        """
        Get root mandate (depth=0) for the orchestrator with multiple scopes.

        This allows a single mandate to cover multiple action/resource pairs,
        enabling unified audit trails and cascade revocation.

        Args:
            principal: The orchestrator principal ID (e.g., "agent:orchestrator")
            scopes: List of scope dicts, each with "action" and "resource" keys
                    e.g., [{"action": "browser.*", "resource": "https://..."},
                           {"action": "fs.*", "resource": "**/workspace/**"}]
            intent_hash: Optional intent hash

        Returns:
            DelegateResponse with root mandate token covering all scopes
        """
        import httpx

        request_body = {
            "principal": principal,
            "scopes": scopes,
            "intent_hash": intent_hash or f"root:{principal}:multi-scope",
            "labels": [],
        }

        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_s) as client:
            response = await client.post("/v1/authorize", json=request_body)

            if response.status_code == 403:
                data = response.json()
                raise RuntimeError(f"Root authorization denied: {data.get('reason', 'unknown')}")

            if not response.is_success:
                raise RuntimeError(f"Root authorization failed: {response.status_code} - {response.text}")

            data = response.json()

            if not data.get("allowed", False):
                raise RuntimeError(f"Root authorization denied: {data.get('reason', 'unknown')}")

            return DelegateResponse(
                mandate_token=data.get("mandate_token", data.get("mandate_id", "")),
                mandate_id=data.get("mandate_id", ""),
                expires_at=data.get("expires_at", 0),
                delegation_depth=0,
                delegation_chain_hash=data.get("delegation_chain_hash", "root"),
                scopes_authorized=data.get("scopes_authorized", []),
            )

    async def delegate(
        self,
        parent_mandate_token: str,
        target_agent_id: str,
        requested_action: str,
        requested_resource: str,
        intent_hash: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
    ) -> DelegateResponse:
        """
        Delegate authority from parent mandate to a child agent.

        The sidecar validates:
        1. Parent mandate signature is valid
        2. Parent mandate is not expired or revoked
        3. Requested scope is a subset of parent's scope
        4. Delegation depth does not exceed maximum

        Args:
            parent_mandate_token: The parent's mandate JWT token
            target_agent_id: The child agent's principal ID
            requested_action: Narrower action scope for the child
            requested_resource: Narrower resource scope for the child
            intent_hash: Optional intent hash
            ttl_seconds: Optional TTL (capped to parent's remaining TTL)

        Returns:
            DelegateResponse with derived mandate token
        """
        import httpx

        request_body = {
            "parent_mandate_token": parent_mandate_token,
            "target_agent_id": target_agent_id,
            "requested_action": requested_action,
            "requested_resource": requested_resource,
            "intent_hash": intent_hash or f"delegate:{target_agent_id}:{requested_action}",
        }

        if ttl_seconds is not None:
            request_body["ttl_seconds"] = ttl_seconds

        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_s) as client:
            response = await client.post("/v1/delegate", json=request_body)

            if response.status_code == 403:
                data = response.json()
                code = data.get("code", "unknown")
                message = data.get("message", "Delegation denied")
                raise RuntimeError(f"Delegation denied [{code}]: {message}")

            if not response.is_success:
                raise RuntimeError(f"Delegation failed: {response.status_code} - {response.text}")

            data = response.json()

            return DelegateResponse(
                mandate_token=data["mandate_token"],
                mandate_id=data["mandate_id"],
                expires_at=data["expires_at"],
                delegation_depth=data["delegation_depth"],
                delegation_chain_hash=data["delegation_chain_hash"],
            )


# Global delegation state (set when --use-delegation is enabled)
_delegation_client: Optional[DelegationClient] = None
_root_mandate: Optional[DelegateResponse] = None
_scraper_mandate: Optional[DelegateResponse] = None
_analyst_mandate: Optional[DelegateResponse] = None


def _build_compact_context(snapshot, goal: Optional[str] = None) -> Optional[str]:
    """
    Build compact DOM context from snapshot using LLMInteractionHandler.

    Format: [ID] <role> "text" {cues} @ (x,y) size:WxH importance:score [status]
    Example: [346] <button> "Add to Cart" {CLICKABLE,color:orange} @ (664,100) size:150x40 importance:811

    Args:
        snapshot: Snapshot object from PredicateDebugger
        goal: Optional goal string for context

    Returns:
        Compact DOM context string, or None if unavailable
    """
    if snapshot is None:
        return None
    if LLMInteractionHandler is None:
        return None

    try:
        # LLMInteractionHandler.build_context is a static-like method that only needs snapshot
        # We create a dummy handler just to use its build_context method
        # Note: build_context doesn't actually use the LLM, just formats elements
        class _DummyProvider:
            pass
        handler = LLMInteractionHandler(_DummyProvider())
        compact_context = handler.build_context(snapshot, goal)
        return compact_context
    except Exception as e:
        print(f"[warn] compact context build failed: {e}", flush=True)
        return None


def _emit_snapshot_trace(
    tracer,
    snapshot,
    step_id: Optional[str],
    step_index: Optional[int],
    compact_context: Optional[str] = None,
) -> None:
    """
    Emit a snapshot trace event with screenshot payload for Studio.

    This sends step data including DOM snapshot, screenshot, and compact DOM context
    to the tracer, which uploads it to Predicate Studio for debugging and observability.

    Args:
        tracer: Tracer instance for emitting events
        snapshot: Snapshot object with elements and screenshot
        step_id: Step ID for correlation
        step_index: Step index in sequence
        compact_context: Optional compact DOM context string from LLMInteractionHandler
    """
    if tracer is None or snapshot is None:
        return
    if TraceEventBuilder is None:
        return

    try:
        data = TraceEventBuilder.build_snapshot_event(snapshot, step_index=step_index)
        screenshot_raw = getattr(snapshot, "screenshot", None)
        if screenshot_raw:
            # Extract base64 string from data URL if needed
            # Format: "data:image/jpeg;base64,{base64_string}"
            if screenshot_raw.startswith("data:image"):
                screenshot_base64 = (
                    screenshot_raw.split(",", 1)[1]
                    if "," in screenshot_raw
                    else screenshot_raw
                )
            else:
                screenshot_base64 = screenshot_raw
            data["screenshot_base64"] = screenshot_base64
            data["screenshot_format"] = SCREENSHOT_FORMAT
        else:
            print("[warn] snapshot has no screenshot", flush=True)

        # Add compact DOM context for LLM-friendly element representation
        if compact_context:
            data["compact_context"] = compact_context
            # Also add element count for quick reference
            element_count = len(getattr(snapshot, "elements", []))
            data["element_count"] = element_count

        tracer.emit("snapshot", data=data, step_id=step_id)
    except Exception:
        # Silently fail like the reference implementation
        return

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
# Browser Lifecycle Management
# =============================================================================

def init_browser_sync(
    tracer,
    predicate_api_key: Optional[str] = None,
    allowed_domains: Optional[list] = None,
) -> tuple:
    """
    Initialize sync PredicateBrowser for CrewAI tool compatibility.

    Uses sync PredicateBrowser instead of async to work with CrewAI's sync tool decorator.

    Returns:
        Tuple of (browser, page, None)  # No debugger in sync mode for now
    """
    global _browser_instance, _page_instance, _tracer_instance

    # Store tracer reference for step data emission
    _tracer_instance = tracer

    if not PREDICATE_SDK_AVAILABLE:
        print("[browser] Predicate SDK not available, falling back to requests-based scraping")
        return None, None, None

    if allowed_domains is None:
        allowed_domains = ["amazon.com", "bestbuy.com", "walmart.com", "newegg.com", "target.com"]

    # Initialize sync browser
    browser = PredicateBrowser(
        api_key=predicate_api_key or "local",
        headless=HEADLESS,
        allowed_domains=allowed_domains,
    )

    browser.start()
    page = browser.page

    if page is None:
        raise RuntimeError("PredicateBrowser did not create a page.")

    _browser_instance = browser
    _page_instance = page

    print(f"[browser] Initialized PredicateBrowser (sync, headless={HEADLESS})")

    return browser, page, None


def close_browser_sync():
    """Close the sync browser instance."""
    global _browser_instance, _page_instance

    if _browser_instance:
        try:
            _browser_instance.close()
            print("[browser] Browser closed")
        except Exception as e:
            print(f"[browser] Error closing browser: {e}")

    _browser_instance = None
    _page_instance = None
    _debugger_instance = None


# =============================================================================
# LLM Configuration (DeepInfra or Ollama - not OpenAI)
# =============================================================================

# Supported LLM providers
# Note: Using Llama 3.1 70B as it's more reliable than Qwen for tool-calling
LLM_PROVIDERS = {
    "deepinfra": {
        "model": "meta-llama/Meta-Llama-3.1-70B-Instruct",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "env_key": "DEEPINFRA_API_KEY",
        "description": "DeepInfra cloud (requires DEEPINFRA_API_KEY)",
    },
    "ollama": {
        "model": "ollama/qwen2.5:7b",
        "base_url": None,  # Will use OLLAMA_HOST env var or default to localhost
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
        Configured CrewAI LLM instance with retry logic for API failures
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

    # Resolve base_url - for Ollama, use OLLAMA_HOST env var (set by docker-compose)
    base_url = config["base_url"]
    if base_url is None and provider == "ollama":
        base_url = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        print(f"[LLM] Ollama base_url: {base_url}")

    # Build LLM kwargs with retry configuration for resilience
    # Handles: empty LLM responses, rate limiting, API quota issues, network timeouts
    llm_kwargs = {
        "model": config["model"],
        "base_url": base_url,
        "temperature": 0.1,
        # Retry configuration for API resilience
        "num_retries": 5,   # Retry up to 5 times on failure
        "timeout": 180,     # 3 minute timeout per request (large models can be slow)
        "max_tokens": 4096, # Ensure we request enough tokens for response
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

def _navigate_with_browser(url: str) -> str:
    """
    Navigate to a product page using sync PredicateBrowser.

    Uses snapshot() for DOM capture with find() for element extraction.
    """
    global _browser_instance, _page_instance, _tracer_instance

    if _page_instance is None:
        return "ERROR: Browser not initialized. Call init_browser_sync() first."

    page = _page_instance
    browser = _browser_instance
    tracer = _tracer_instance

    verification_results = []

    try:
        # Navigate using sync Playwright page
        page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
        final_url = page.url

        # Take snapshot using sync snapshot() function
        if snapshot is not None and browser is not None:
            snap = snapshot(browser)
            element_count = len(getattr(snap, "elements", []))
            print(f"[snapshot] Navigate captured {element_count} elements")

            # Build compact DOM context for LLM-friendly element representation
            compact_context = _build_compact_context(snap, goal=f"navigate:{url}")
            if compact_context:
                print(f"[compact] Built compact DOM context: {element_count} elements")
                print(f"[compact] --- Compact DOM Context ---")
                # Print first 80 lines to keep logs readable
                context_lines = compact_context.split('\n')
                for line in context_lines[:80]:
                    print(f"[compact] {line}")
                if len(context_lines) > 80:
                    print(f"[compact] ... ({len(context_lines) - 80} more lines)")
                print(f"[compact] --- End Compact DOM Context ---")

            # Emit step data to tracer for Studio (includes compact context)
            _emit_snapshot_trace(
                tracer,
                snap,
                None,  # No step_id in sync mode
                0,     # step_index
                compact_context=compact_context,
            )

            # Verify using find() on snapshot
            if "amazon.com" in url:
                url_check = "/dp/" in final_url or "/gp/product/" in final_url
                verification_results.append(f"url_contains(/dp/): {'PASS' if url_check else 'FAIL'}")

                # Check for product title using find()
                title_el = find(snap, "role=heading") if find else None
                verification_results.append(f"find(role=heading): {'PASS' if title_el else 'FAIL'}")

                # Check for price element using find()
                price_el = find(snap, "text~'$'") if find else None
                verification_results.append(f"find(text~'$'): {'PASS' if price_el else 'FAIL'}")

            elif "bestbuy.com" in url:
                url_check = "/site/" in final_url
                verification_results.append(f"url_contains(/site/): {'PASS' if url_check else 'FAIL'}")

            elif "walmart.com" in url:
                url_check = "/ip/" in final_url
                verification_results.append(f"url_contains(/ip/): {'PASS' if url_check else 'FAIL'}")
        else:
            verification_results.append(f"navigation: PASS (final_url={final_url})")

        all_passed = all("PASS" in r or "SKIP" in r for r in verification_results)

        if all_passed:
            return f"SUCCESS: Navigated to {url}\nFinal URL: {final_url}\nVerification:\n" + "\n".join(verification_results)
        else:
            return f"WARNING: Navigated but verification issues at {url}\nFinal URL: {final_url}\nVerification:\n" + "\n".join(verification_results)

    except Exception as e:
        return f"ERROR: Browser navigation failed for {url}: {str(e)}"


def _navigate_with_requests(url: str) -> str:
    """
    Navigate to a product page using requests (sync fallback).
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        response = requests.get(url, headers=headers, timeout=10, allow_redirects=True)

        verification_results = []
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
            verification_results.append("url_contains: SKIP (no pattern for domain)")

        status_ok = response.status_code == 200
        verification_results.append(f"http_status(200): {'PASS' if status_ok else f'FAIL ({response.status_code})'}")

        html_content = response.text
        if "amazon.com" in url:
            title_exists = 'id="productTitle"' in html_content or 'id="title"' in html_content
            verification_results.append(f"exists(#productTitle): {'PASS' if title_exists else 'FAIL'}")

            is_404_page = "Sorry, we couldn't find that page" in html_content or \
                          "looking for something" in html_content.lower() and "dogs" in html_content.lower()
            if is_404_page:
                verification_results.append("not_exists(404_page): FAIL - Product not found")
                return f"ERROR: Product page not found at {url}\nVerification:\n" + "\n".join(verification_results)

            price_exists = 'class="a-price' in html_content or 'id="priceblock' in html_content or \
                           'a-offscreen' in html_content
            verification_results.append(f"exists(.a-price): {'PASS' if price_exists else 'FAIL (may be CAPTCHA)'}")

        elif "bestbuy.com" in url:
            title_exists = 'class="sku-title"' in html_content or 'class="heading-5"' in html_content
            verification_results.append(f"exists(.sku-title): {'PASS' if title_exists else 'FAIL'}")

        elif "walmart.com" in url:
            title_exists = 'itemprop="name"' in html_content
            verification_results.append(f"exists([itemprop=name]): {'PASS' if title_exists else 'FAIL'}")

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

    # Use sync PredicateBrowser if initialized, otherwise fall back to requests
    if _browser_instance is not None and _page_instance is not None:
        try:
            return _navigate_with_browser(url)
        except Exception as e:
            print(f"[navigate] Browser failed, falling back to requests: {e}")
            return _navigate_with_requests(url)
    else:
        return _navigate_with_requests(url)


def _extract_with_browser(url: str) -> str:
    """
    Extract price data using sync PredicateBrowser with find() on snapshots.

    Uses snapshot() for DOM capture with find() for semantic element queries.
    Emits step data to tracer for Predicate Studio observability.
    """
    global _browser_instance, _page_instance, _tracer_instance

    if _page_instance is None:
        return json.dumps({"url": url, "error": "Browser not initialized"}, indent=2)

    page = _page_instance
    browser = _browser_instance
    tracer = _tracer_instance

    verification_results = []

    extracted_data = {
        "url": url,
        "final_url": page.url,
        "timestamp": datetime.now().isoformat(),
        "product_name": None,
        "price": None,
        "currency": "USD",
        "availability": None,
        "verification": {},
        "snapshot_captured": False,
        "extraction_method": "predicate_find",
    }

    try:
        # Take snapshot using sync snapshot() function
        if snapshot is not None and browser is not None:
            snap = snapshot(browser)
            element_count = len(getattr(snap, "elements", []))
            extracted_data["element_count"] = element_count
            extracted_data["snapshot_captured"] = True
            print(f"[snapshot] Extract captured {element_count} elements")

            # Build compact DOM context for LLM-friendly element representation
            compact_context = _build_compact_context(snap, goal=f"extract:{url}")
            if compact_context:
                print(f"[compact] Built compact DOM context: {element_count} elements")
                extracted_data["compact_element_count"] = element_count
                print(f"[compact] --- Compact DOM Context (Extract) ---")
                # Print first 80 lines to keep logs readable
                context_lines = compact_context.split('\n')
                for line in context_lines[:80]:
                    print(f"[compact] {line}")
                if len(context_lines) > 80:
                    print(f"[compact] ... ({len(context_lines) - 80} more lines)")
                print(f"[compact] --- End Compact DOM Context ---")

            # Emit step data to tracer for Studio (includes compact context)
            _emit_snapshot_trace(
                tracer,
                snap,
                None,  # No step_id in sync mode
                1,     # step_index
                compact_context=compact_context,
            )

            # Extract data using find() on snapshot elements
            if "amazon.com" in url and find is not None:
                # Find product title using semantic query
                title_el = find(snap, "role=heading") or find(snap, "text~'productTitle'")
                if title_el:
                    extracted_data["product_name"] = title_el.text.strip() if title_el.text else None
                    verification_results.append(f"find(role=heading): PASS (id={title_el.id})")
                else:
                    # Fallback: look for any prominent text element
                    for el in snap.elements:
                        if el.role == "heading" or (el.importance and el.importance > 500):
                            if el.text and len(el.text) > 10:
                                extracted_data["product_name"] = el.text.strip()
                                verification_results.append(f"find(importance>500): PASS (id={el.id})")
                                break
                    if not extracted_data["product_name"]:
                        verification_results.append("find(role=heading): FAIL")

                # Find price using semantic query - look for text containing $
                price_el = find(snap, "text~'$'")
                if price_el and price_el.text:
                    price_match = re.search(r'\$?([\d,]+\.?\d*)', price_el.text)
                    if price_match:
                        extracted_data["price"] = float(price_match.group(1).replace(",", ""))
                        verification_results.append(f"find(text~'$'): PASS (${extracted_data['price']}, id={price_el.id})")

                if extracted_data["price"] is None:
                    # Fallback: scan all elements for price pattern
                    for el in snap.elements:
                        if el.text and '$' in el.text:
                            price_match = re.search(r'\$([\d,]+\.?\d{2})', el.text)
                            if price_match:
                                extracted_data["price"] = float(price_match.group(1).replace(",", ""))
                                verification_results.append(f"find(text contains $): PASS (${extracted_data['price']}, id={el.id})")
                                break

                if extracted_data["price"] is None:
                    verification_results.append("find(price): FAIL - No price found in snapshot")

                # Check availability by scanning element text
                for el in snap.elements:
                    if el.text:
                        text_lower = el.text.lower()
                        if "in stock" in text_lower:
                            extracted_data["availability"] = "In Stock"
                            verification_results.append(f"find(text~'In Stock'): PASS (id={el.id})")
                            break
                        elif "out of stock" in text_lower or "unavailable" in text_lower:
                            extracted_data["availability"] = "Out of Stock"
                            verification_results.append(f"find(text~'Out of Stock'): PASS (id={el.id})")
                            break

                if not extracted_data["availability"]:
                    extracted_data["availability"] = "Unknown"
                    verification_results.append("find(availability): UNKNOWN")

            elif "bestbuy.com" in url and find is not None:
                title_el = find(snap, "role=heading")
                if title_el and title_el.text:
                    extracted_data["product_name"] = title_el.text.strip()
                    verification_results.append(f"find(role=heading): PASS (id={title_el.id})")

                price_el = find(snap, "text~'$'")
                if price_el and price_el.text:
                    price_match = re.search(r'\$?([\d,]+\.?\d*)', price_el.text)
                    if price_match:
                        extracted_data["price"] = float(price_match.group(1).replace(",", ""))
                        verification_results.append(f"find(text~'$'): PASS (${extracted_data['price']})")

            elif "walmart.com" in url and find is not None:
                title_el = find(snap, "role=heading")
                if title_el and title_el.text:
                    extracted_data["product_name"] = title_el.text.strip()
                    verification_results.append(f"find(role=heading): PASS (id={title_el.id})")

                price_el = find(snap, "text~'$'")
                if price_el and price_el.text:
                    price_match = re.search(r'([\d.]+)', price_el.text)
                    if price_match:
                        extracted_data["price"] = float(price_match.group(1))
                        verification_results.append(f"find(text~'$'): PASS (${extracted_data['price']})")

            verification_results.append("response_not_empty: PASS")
        else:
            # No snapshot function, fall back to page.evaluate for extraction
            extracted_data["extraction_method"] = "page_evaluate"
            if "amazon.com" in url:
                title_text = page.evaluate("() => document.querySelector('#productTitle')?.textContent?.trim() || ''")
                if title_text:
                    extracted_data["product_name"] = title_text
                    verification_results.append("page.evaluate(#productTitle): PASS")

                price_text = page.evaluate("() => document.querySelector('.a-price .a-offscreen')?.textContent?.trim() || ''")
                if price_text:
                    price_match = re.search(r'\$?([\d,]+\.?\d*)', price_text)
                    if price_match:
                        extracted_data["price"] = float(price_match.group(1).replace(",", ""))
                        verification_results.append(f"page.evaluate(.a-price): PASS (${extracted_data['price']})")

        extracted_data["verification"] = {
            "checks": verification_results,
            "all_passed": all("PASS" in r for r in verification_results) if verification_results else False,
            "method": extracted_data["extraction_method"],
        }

        return json.dumps(extracted_data, indent=2)

    except Exception as e:
        return json.dumps({
            "url": url,
            "error": str(e),
            "verification": {"checks": [f"predicate_find_extraction: FAIL - {str(e)}"], "all_passed": False}
        }, indent=2)


def _extract_with_requests(url: str) -> str:
    """
    Extract price data using requests (sync fallback).
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
            title_match = re.search(r'id="productTitle"[^>]*>([^<]+)<', html_content)
            if title_match:
                extracted_data["product_name"] = title_match.group(1).strip()
                verification_results.append("exists(#productTitle): PASS")
            else:
                title_match = re.search(r'id="title"[^>]*>([^<]+)<', html_content)
                if title_match:
                    extracted_data["product_name"] = title_match.group(1).strip()
                    verification_results.append("exists(#title): PASS")
                else:
                    verification_results.append("exists(#productTitle): FAIL")

            price_patterns = [
                r'class="a-price-whole">(\d+)</span>',
                r'class="a-offscreen">\$?([\d,]+\.?\d*)</span>',
                r'id="priceblock_ourprice"[^>]*>\$?([\d,]+\.?\d*)',
                r'id="priceblock_dealprice"[^>]*>\$?([\d,]+\.?\d*)',
                r'"price":"([\d.]+)"',
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

            if "In Stock" in html_content or "in stock" in html_content.lower():
                extracted_data["availability"] = "In Stock"
                verification_results.append("dom_contains('In Stock'): PASS")
            elif "Out of Stock" in html_content or "Currently unavailable" in html_content:
                extracted_data["availability"] = "Out of Stock"
                verification_results.append("dom_contains('Out of Stock'): PASS")
            else:
                extracted_data["availability"] = "Unknown"
                verification_results.append("dom_contains(availability): FAIL - Unknown status")

            if "Enter the characters you see below" in html_content or "captcha" in html_content.lower():
                verification_results.append("not_exists(captcha): FAIL - CAPTCHA detected")
                extracted_data["error"] = "CAPTCHA detected"

            if "Sorry, we couldn't find that page" in html_content or \
               ("looking for something" in html_content.lower() and "dogs" in html_content.lower()):
                verification_results.append("not_exists(404_page): FAIL - Product not found")
                extracted_data["error"] = "Product not found (404)"

        elif "bestbuy.com" in url:
            title_match = re.search(r'class="sku-title"[^>]*>([^<]+)<', html_content)
            if title_match:
                extracted_data["product_name"] = title_match.group(1).strip()
                verification_results.append("exists(.sku-title): PASS")

            price_match = re.search(r'class="priceView-customer-price"[^>]*>\$?([\d,]+\.?\d*)', html_content)
            if price_match:
                extracted_data["price"] = float(price_match.group(1).replace(",", ""))
                verification_results.append(f"exists(.priceView-customer-price): PASS (${extracted_data['price']})")

        elif "walmart.com" in url:
            title_match = re.search(r'itemprop="name"[^>]*>([^<]+)<', html_content)
            if title_match:
                extracted_data["product_name"] = title_match.group(1).strip()
                verification_results.append("exists([itemprop=name]): PASS")

            price_match = re.search(r'itemprop="price"[^>]*content="([\d.]+)"', html_content)
            if price_match:
                extracted_data["price"] = float(price_match.group(1))
                verification_results.append(f"exists([itemprop=price]): PASS (${extracted_data['price']})")

        extracted_data["verification"] = {
            "checks": verification_results,
            "all_passed": all("PASS" in r for r in verification_results) if verification_results else False,
            "response_not_empty": len(html_content) > 1000,
            "method": "requests",
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
def extract_price_data(url: str) -> str:
    """
    Extract price and availability data from the current product page.

    Args:
        url: The product URL to extract data from

    Returns:
        JSON string with price, availability, and product info, plus verification results
    """
    # Use sync PredicateBrowser with find() if initialized, otherwise fall back to requests
    if _browser_instance is not None and _page_instance is not None:
        try:
            return _extract_with_browser(url)
        except Exception as e:
            print(f"[extract] Browser failed, falling back to requests: {e}")
            return _extract_with_requests(url)
    else:
        return _extract_with_requests(url)


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
        "tablet": "B0CK3RQQ38",       # iPad 10th Gen (64GB Blue)
        "phone": "B0DHJH2GZL",        # iPhone 16 128GB Black
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

async def async_main():
    """Async main function for browser-based scraping with snapshots."""
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
    parser.add_argument(
        "--use-browser",
        action="store_true",
        default=False,
        help="Use Playwright browser with snapshots instead of requests (requires predicate SDK)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=True,
        help="Run browser in headless mode (default: True)",
    )
    parser.add_argument(
        "--use-delegation",
        action="store_true",
        default=False,
        help="Enable chain delegation: orchestrator delegates to scraper/analyst agents",
    )
    args = parser.parse_args()

    # Update global headless setting
    global HEADLESS
    HEADLESS = args.headless

    products = [p.strip() for p in args.products.split(",")]
    run_id = str(uuid.uuid4())
    goal = f"Monitor e-commerce prices for: {', '.join(products)}"
    predicate_api_key = os.getenv("PREDICATE_API_KEY")

    print("=" * 70)
    print("Zero-Trust Multi-Agent E-commerce Price Monitoring System")
    print("=" * 70)
    print(f"Run ID: {run_id}")
    print(f"Products: {products}")
    print(f"Policy: {args.policy}")
    print(f"Mode: {args.mode}")
    print(f"Sidecar URL: {args.sidecar_url}")
    print(f"LLM Provider: {args.llm}")
    print(f"Use Browser: {args.use_browser}")
    print(f"Headless: {args.headless}")
    print(f"Use Delegation: {args.use_delegation}")
    print(f"Predicate SDK: {'Available' if PREDICATE_SDK_AVAILABLE else 'Not Available'}")
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
                    "use_browser": args.use_browser,
                    "use_delegation": args.use_delegation,
                    "headless": args.headless,
                },
            )
        except Exception as e:
            print(f"[warn] tracer emit_run_start failed: {e}")

    # Browser is initialized in main() sync wrapper before async context
    # Check if browser is already initialized (via global instances)
    browser = _browser_instance
    page = _page_instance
    dbg = None  # No debugger in sync mode

    if args.use_browser:
        if browser is not None and page is not None:
            # Update tracer reference for the already-initialized browser
            global _tracer_instance
            _tracer_instance = tracer
        elif not PREDICATE_SDK_AVAILABLE:
            print("[browser] --use-browser specified but Predicate SDK not available")
            print("[browser] Install with: pip install predicate-runtime")
            print("[browser] Falling back to requests-based scraping")

    # Chain delegation state
    global _delegation_client, _root_mandate, _scraper_mandate, _analyst_mandate

    try:
        # Create base agents
        web_scraper, analyst = create_agents(llm)

        # =========================================================================
        # Chain Delegation Flow (when --use-delegation is enabled)
        # =========================================================================
        #
        # This implements the architecture diagram at the top of main.py:
        #
        #   ┌─────────────────────────────────────────────────────────────────────┐
        #   │                  POST /v1/authorize (root mandate)                   │
        #   │   Orchestrator: browser.*, fs.*, tool.* on workspace/**              │
        #   └───────────────────────────────┬─────────────────────────────────────┘
        #                                   │
        #           ┌───────────────────────┴───────────────────────┐
        #           ▼                                               ▼
        #   ┌───────────────────────┐                     ┌───────────────────────┐
        #   │  POST /v1/delegate    │                     │  POST /v1/delegate    │
        #   │  parent: root mandate │                     │  parent: root mandate │
        #   │  target: agent:scraper│                     │  target: agent:analyst│
        #   │  scope: browser.*,    │                     │  scope: fs.read,      │
        #   │         fs.write      │                     │         fs.write,     │
        #   │         scraped/**    │                     │         tool.*        │
        #   └───────────────────────┘                     └───────────────────────┘
        #
        # Benefits:
        # - Orchestrator has broad scope, agents have minimal required permissions
        # - Cascading revocation: revoking orchestrator revokes all derived mandates
        # - Cryptographic proof: delegation_chain_hash links child to parent
        # - Scope narrowing: child cannot escalate beyond parent's scope
        #
        if args.use_delegation:
            print("\n" + "=" * 70)
            print("[Chain Delegation] Initializing orchestrator → agent delegation")
            print("=" * 70)

            _delegation_client = DelegationClient(base_url=args.sidecar_url)

            # Step 1: Get multi-scope root mandate for orchestrator
            # Multi-scope mandates allow a single mandate to cover browser + fs scopes,
            # providing unified audit trail and cascade revocation.
            print("\n[Delegation] Step 1: Requesting multi-scope root mandate for orchestrator...")

            try:
                _root_mandate = await _delegation_client.authorize_root_multi_scope(
                    principal="agent:orchestrator",
                    scopes=[
                        {"action": "browser.*", "resource": "https://www.amazon.com/*"},
                        {"action": "fs.*", "resource": "**/workspace/data/**"},
                    ],
                    intent_hash=f"orchestrate:ecommerce:{run_id}",
                )
                print(f"  ✓ Multi-scope root mandate issued:")
                print(f"    - mandate_id: {_root_mandate.mandate_id}")
                print(f"    - scopes: browser.* + fs.* (unified mandate)")
                if _root_mandate.scopes_authorized:
                    for scope in _root_mandate.scopes_authorized:
                        print(f"      • {scope.get('action')} on {scope.get('resource')} (rule: {scope.get('matched_rule', 'n/a')})")
            except Exception as e:
                print(f"  ✗ Multi-scope root mandate failed: {e}")
                print("  → Falling back to direct authorization (no delegation)")
                _root_mandate = None
                args.use_delegation = False

        if args.use_delegation and _root_mandate:
            # Step 2: Delegate to scraper agent (browser scope from multi-scope parent)
            print("\n[Delegation] Step 2: Delegating to agent:scraper...")
            try:
                _scraper_mandate = await _delegation_client.delegate(
                    parent_mandate_token=_root_mandate.mandate_token,
                    target_agent_id="agent:scraper",
                    requested_action="browser.*",
                    requested_resource="https://www.amazon.com/*",
                    intent_hash=f"scrape:{run_id}",
                    ttl_seconds=300,
                )
                print(f"  ✓ Scraper mandate issued:")
                print(f"    - mandate_id: {_scraper_mandate.mandate_id}")
                print(f"    - depth: {_scraper_mandate.delegation_depth}")
                print(f"    - chain_hash: {_scraper_mandate.delegation_chain_hash[:16]}...")
                print(f"    - parent: {_root_mandate.mandate_id} (multi-scope)")
            except Exception as e:
                print(f"  ✗ Scraper delegation failed: {e}")
                print("  → Scraper will use direct authorization")

            # Step 3: Delegate to analyst agent (fs scope from same multi-scope parent)
            print("\n[Delegation] Step 3: Delegating to agent:analyst...")
            try:
                _analyst_mandate = await _delegation_client.delegate(
                    parent_mandate_token=_root_mandate.mandate_token,  # Same parent!
                    target_agent_id="agent:analyst",
                    requested_action="fs.*",
                    requested_resource="**/workspace/data/**",
                    intent_hash=f"analyze:{run_id}",
                    ttl_seconds=300,
                )
                print(f"  ✓ Analyst mandate issued:")
                print(f"    - mandate_id: {_analyst_mandate.mandate_id}")
                print(f"    - depth: {_analyst_mandate.delegation_depth}")
                print(f"    - chain_hash: {_analyst_mandate.delegation_chain_hash[:16]}...")
                print(f"    - parent: {_root_mandate.mandate_id} (multi-scope)")
            except Exception as e:
                print(f"  ✗ Analyst delegation failed: {e}")
                print("  → Analyst will use direct authorization")

            print("\n[Delegation] Chain delegation complete!")
            print("  → Single multi-scope mandate enables unified audit trail")
            print("  → Revoking root mandate will cascade to all child delegations")
            print("-" * 70)

        # Wrap agents with SecureAgent for zero-trust enforcement
        print(f"\n[SecureAgent] Initializing with policy: {args.policy}")
        print(f"[SecureAgent] Mode: {args.mode} (fail-closed)")
        if args.use_delegation:
            print(f"[SecureAgent] Delegation: Enabled (agents have derived mandates)")

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
        print(f"  - Method: {'Playwright browser' if browser else 'HTTP requests'}")
        if dbg:
            print(f"  - Snapshots: Enabled with screenshots")
        if args.use_delegation:
            print(f"  - Chain Delegation: Enabled")
            if _root_mandate:
                print(f"    - Root mandate: {_root_mandate.mandate_id}")
            if _scraper_mandate:
                print(f"    - Scraper mandate: {_scraper_mandate.mandate_id} (depth={_scraper_mandate.delegation_depth})")
            if _analyst_mandate:
                print(f"    - Analyst mandate: {_analyst_mandate.mandate_id} (depth={_analyst_mandate.delegation_depth})")
        print("=" * 70)

    finally:
        # Close browser if initialized
        if browser:
            close_browser_sync()

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
                        "method": "playwright" if browser else "requests",
                        "delegation": args.use_delegation,
                        "delegation_chain": {
                            "root": _root_mandate.mandate_id if _root_mandate else None,
                            "scraper": _scraper_mandate.mandate_id if _scraper_mandate else None,
                            "analyst": _analyst_mandate.mandate_id if _analyst_mandate else None,
                        } if args.use_delegation else None,
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


def main():
    """
    Sync main entry point.

    Uses nest_asyncio to allow nested event loops, since Playwright's sync API
    uses an internal event loop which conflicts with asyncio.run().
    """
    import argparse

    # Enable nested event loops for Playwright sync + asyncio.run() compatibility
    try:
        import nest_asyncio
        nest_asyncio.apply()
    except ImportError:
        pass  # Will work without it if browser mode is not used

    parser = argparse.ArgumentParser(
        description="Zero-Trust E-commerce Price Monitoring with CrewAI + Predicate Secure"
    )
    parser.add_argument("--use-browser", action="store_true", help="Use Playwright browser")
    parser.add_argument("--headless", action="store_true", default=True, help="Run headless")
    # Parse just the browser args to check if we need browser
    args, _ = parser.parse_known_args()

    # Initialize sync browser BEFORE entering async context
    if args.use_browser and PREDICATE_SDK_AVAILABLE:
        try:
            predicate_api_key = os.environ.get("PREDICATE_API_KEY")
            browser, page, _ = init_browser_sync(
                tracer=None,  # Tracer set later in async_main
                predicate_api_key=predicate_api_key,
                allowed_domains=["amazon.com", "bestbuy.com", "walmart.com", "newegg.com", "target.com"],
            )
            print(f"[browser] PredicateBrowser (sync) initialized with snapshot + find() support")
            print(f"[browser] Extraction method: predicate_find (semantic element queries)")
        except Exception as e:
            print(f"[browser] Failed to initialize browser: {e}")
            print("[browser] Falling back to requests-based scraping")

    # Now run the async main (browser already initialized outside async context)
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
