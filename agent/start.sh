#!/bin/bash
#
# Agent Start Script for Trump AI LiveKit
# Run this script from within the agent/ directory
#

set -e  # Exit on error

echo "=================================================="
echo "Trump AI LiveKit - Agent Server Startup"
echo "=================================================="

# Color codes for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Function to print colored messages
print_success() { echo -e "${GREEN}✓ $1${NC}"; }
print_warning() { echo -e "${YELLOW}⚠ $1${NC}"; }
print_error() { echo -e "${RED}✗ $1${NC}"; }

# Check if we're in the agent directory
if [ ! -f "main.py" ] || [ ! -f "pyproject.toml" ]; then
    print_error "Please run this script from the agent/ directory!"
    echo "Usage: cd agent && ./start-agent.sh"
    exit 1
fi

# Check if .env file exists
if [ ! -f ".env" ]; then
    print_error ".env file not found!"
    echo "Please create .env file with required API keys:"
    echo "  - LIVEKIT_API_KEY"
    echo "  - LIVEKIT_API_SECRET"
    echo "  - CARTESIA_API_KEY"
    echo "  - DASHSCOPE_API_KEY"
    exit 1
fi

print_success ".env file found"

# Check if Python 3.9+ is installed
if ! command -v python3 &> /dev/null; then
    print_error "Python 3 is not installed. Please install Python 3.9 or higher."
    exit 1
fi

PYTHON_VERSION=$(python3 --version | awk '{print $2}')
print_success "Python $PYTHON_VERSION found"

# Load environment variables
export $(grep -v '^#' .env | xargs)

# Check required environment variables
if [ -z "$LIVEKIT_API_KEY" ] || [ -z "$LIVEKIT_API_SECRET" ]; then
    print_error "LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set in .env"
    exit 1
fi

print_success "LiveKit credentials found"

# Check if LiveKit is already running
echo ""
echo "Checking if LiveKit server is running..."
if ! curl -s http://localhost:8002/ > /dev/null 2>&1; then
    print_warning "LiveKit server doesn't seem to be running on port 8002"
    print_warning "Make sure LiveKit Docker container is running before starting the agent"
    echo ""
else
    print_success "LiveKit server is running!"
fi

# Set up Python virtual environment
echo ""
echo "Step 1: Setting up Python environment..."

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    print_error "uv is not installed. Please install uv first:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "  source \$HOME/.cargo/env"
    exit 1
fi

# Create virtual environment if it doesn't exist
if [ ! -d ".venv" ]; then
    print_warning "Creating Python virtual environment with uv..."
    uv venv
    print_success "Virtual environment created"
fi

# Activate virtual environment
source .venv/bin/activate

# Install dependencies using uv
print_warning "Installing Python dependencies with uv (this may take 1-3 minutes)..."
uv sync --quiet
print_success "Python dependencies installed"

# Set environment to production
export ENV=PRODUCTION-SILICON

# Start the agent
echo ""
echo "Step 2: Starting Python agent..."
echo ""
print_warning "Agent is starting in production mode (ENV=PRODUCTION-SILICON)..."
print_warning "Check .config file to ensure LIVEKIT_URL is correct"
echo ""

# Create log directory structure as specified in .config
LOG_DIR="logs"
mkdir -p "$LOG_DIR"

# Generate log filename with current date (matching .config LOG_FILE_PATH format)
LOG_FILE="$LOG_DIR/agent_$(date +%Y-%m-%d).log"

print_success "Log directory created: $LOG_DIR"
print_warning "Logs will be saved to: $LOG_FILE"

# Run the agent in background with 'start' command
nohup python main.py start >> "$LOG_FILE" 2>&1 &
AGENT_PID=$!

print_success "Agent started in background (PID: $AGENT_PID)"
echo ""
echo "📋 Useful commands:"
echo "  - View logs: tail -f $LOG_FILE"
echo "  - Stop agent: kill $AGENT_PID"
echo "  - Check status: ps -p $AGENT_PID"
echo ""

# Save PID to file for easy management
echo $AGENT_PID > .agent.pid
print_success "Agent PID saved to .agent.pid"

