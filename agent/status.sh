#!/bin/bash
#
# Check Agent Status Script for Trump AI LiveKit
#

# Color codes
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

print_success() { echo -e "${GREEN}✓ $1${NC}"; }
print_warning() { echo -e "${YELLOW}⚠ $1${NC}"; }
print_error() { echo -e "${RED}✗ $1${NC}"; }
print_info() { echo -e "${BLUE}ℹ $1${NC}"; }

echo "=================================================="
echo "Trump AI LiveKit - Agent Status"
echo "=================================================="
echo ""

# Check if PID file exists
if [ ! -f ".agent.pid" ]; then
    print_error "No PID file found"
    print_info "Agent is not running (or was not started with start.sh)"
    echo ""
    
    # Check for any running python main.py processes
    RUNNING=$(ps aux | grep 'python.*main.py' | grep -v grep)
    if [ -n "$RUNNING" ]; then
        print_warning "Found running Python processes:"
        echo "$RUNNING"
    fi
    exit 1
fi

# Read PID
AGENT_PID=$(cat .agent.pid)

# Check if process is running
if ps -p $AGENT_PID > /dev/null 2>&1; then
    print_success "Agent is RUNNING (PID: $AGENT_PID)"
    echo ""
    
    # Show process details
    print_info "Process details:"
    ps -f -p $AGENT_PID
    echo ""
    
    # Show log file
    LOG_DIR="logs/trump-ai-livekit"
    LOG_FILE="$LOG_DIR/agent_$(date +%Y-%m-%d).log"
    
    if [ -f "$LOG_FILE" ]; then
        print_info "Log file: $LOG_FILE"
        FILE_SIZE=$(du -h "$LOG_FILE" | cut -f1)
        print_info "Log size: $FILE_SIZE"
        echo ""
        print_info "Last 10 lines of log:"
        echo "---"
        tail -n 10 "$LOG_FILE"
        echo "---"
    fi
    
else
    print_error "Agent is NOT RUNNING (stale PID: $AGENT_PID)"
    rm .agent.pid
    print_success "Removed stale PID file"
    exit 1
fi

