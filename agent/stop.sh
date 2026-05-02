#!/bin/bash
#
# Stop Agent Script for Trump AI LiveKit
#

# Color codes
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

print_success() { echo -e "${GREEN}✓ $1${NC}"; }
print_warning() { echo -e "${YELLOW}⚠ $1${NC}"; }
print_error() { echo -e "${RED}✗ $1${NC}"; }

echo "=================================================="
echo "Trump AI LiveKit - Stop Agent"
echo "=================================================="

# Check if PID file exists
if [ ! -f ".agent.pid" ]; then
    print_error "No PID file found. Agent might not be running."
    echo "Use: ps aux | grep 'python main.py' to find the process manually"
    exit 1
fi

# Read PID
AGENT_PID=$(cat .agent.pid)

# Check if process is running
if ! ps -p $AGENT_PID > /dev/null 2>&1; then
    print_warning "Agent process (PID: $AGENT_PID) is not running"
    rm .agent.pid
    print_success "Removed stale PID file"
    exit 0
fi

# Stop the agent
print_warning "Stopping agent (PID: $AGENT_PID)..."
kill $AGENT_PID

# Wait for process to stop
sleep 2

# Check if it stopped
if ps -p $AGENT_PID > /dev/null 2>&1; then
    print_warning "Process still running, forcing stop..."
    kill -9 $AGENT_PID
    sleep 1
fi

# Verify it stopped
if ! ps -p $AGENT_PID > /dev/null 2>&1; then
    print_success "Agent stopped successfully"
    rm .agent.pid
    print_success "Removed PID file"
else
    print_error "Failed to stop agent. Try manually: kill -9 $AGENT_PID"
    exit 1
fi

