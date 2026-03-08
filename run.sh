#!/bin/bash
# Run the CrewAI E-commerce Demo with Docker Compose
#
# Usage:
#   ./run.sh                    # Run with default settings
#   ./run.sh --ollama           # Use local Ollama instead of DeepInfra
#   ./run.sh --debug            # Enable debug logging
#   ./run.sh --audit            # Run in audit mode (log but don't block)
#   ./run.sh --use-browser      # Use Playwright browser with snapshots
#   ./run.sh --rebuild          # Force rebuild containers
#   ./run.sh --down             # Stop and remove containers

set -e

cd "$(dirname "$0")"

# Default values
REBUILD=""
EXTRA_ARGS=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --ollama)
            export LLM_PROVIDER=ollama
            echo "[run] Using local Ollama"
            shift
            ;;
        --debug)
            export LOG_LEVEL=debug
            echo "[run] Debug logging enabled"
            shift
            ;;
        --audit)
            export MODE=audit
            echo "[run] Audit mode (log only, no blocking)"
            shift
            ;;
        --use-browser)
            export USE_BROWSER=true
            echo "[run] Playwright browser mode with snapshots enabled"
            shift
            ;;
        --rebuild)
            REBUILD="yes"
            echo "[run] Forcing container rebuild"
            shift
            ;;
        --down)
            echo "[run] Stopping containers..."
            docker compose down -v
            exit 0
            ;;
        --help|-h)
            echo "Usage: ./run.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --ollama      Use local Ollama instead of DeepInfra"
            echo "  --debug       Enable debug logging"
            echo "  --audit       Run in audit mode (log but don't block)"
            echo "  --use-browser Use Playwright browser with snapshots"
            echo "  --rebuild     Force rebuild containers"
            echo "  --down        Stop and remove containers"
            echo "  --help        Show this help message"
            echo ""
            echo "Environment variables:"
            echo "  PRODUCTS              Comma-separated list of products to monitor"
            echo "  DEEPINFRA_API_KEY     API key for DeepInfra (cloud LLM)"
            echo "  LLM_PROVIDER          LLM provider: auto, deepinfra, ollama"
            echo "  MODE                  Security mode: strict, audit"
            echo "  LOG_LEVEL             Log level: debug, info, warn, error"
            echo "  USE_BROWSER           Set to 'true' to use Playwright browser"
            exit 0
            ;;
        *)
            echo "[run] Unknown option: $1"
            exit 1
            ;;
    esac
done

# Check for .env file
if [ ! -f .env ]; then
    echo "[run] Warning: .env file not found"
    echo "[run] Copy .env.example to .env and configure your settings"
    echo ""
    if [ -z "$DEEPINFRA_API_KEY" ] && [ "$LLM_PROVIDER" != "ollama" ]; then
        echo "[run] Hint: Set DEEPINFRA_API_KEY or use --ollama flag"
        exit 1
    fi
fi

# If using Ollama, verify it's running
if [ "$LLM_PROVIDER" = "ollama" ]; then
    echo "[run] Checking Ollama availability..."
    if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
        echo "[run] Error: Ollama is not running on localhost:11434"
        echo "[run] Start Ollama with: ollama serve"
        exit 1
    fi
    echo "[run] Ollama is available"
fi

# Run docker compose
echo "[run] Starting CrewAI E-commerce Demo..."
echo ""

if [ "$REBUILD" = "yes" ]; then
    # Force rebuild without cache
    docker compose build --no-cache
    docker compose up
else
    docker compose up --build
fi
