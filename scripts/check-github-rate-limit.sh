#!/bin/bash
# Check GitHub API rate limit status

if [ ! -f "GITHUB_TOKEN" ]; then
    echo "Error: GITHUB_TOKEN file not found"
    exit 1
fi

TOKEN=$(cat GITHUB_TOKEN)
RESPONSE=$(curl -s -H "Authorization: token $TOKEN" https://api.github.com/rate_limit)

echo "GitHub API Rate Limit Status:"
echo "=============================="
echo "$RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$RESPONSE"

# Extract reset time
RESET_TIME=$(echo "$RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data['rate']['reset'])" 2>/dev/null)

if [ -n "$RESET_TIME" ]; then
    CURRENT_TIME=$(date +%s)
    SECONDS_UNTIL_RESET=$((RESET_TIME - CURRENT_TIME))
    MINUTES_UNTIL_RESET=$((SECONDS_UNTIL_RESET / 60))
    
    if [ $SECONDS_UNTIL_RESET -gt 0 ]; then
        echo ""
        echo "Rate limit resets in: $MINUTES_UNTIL_RESET minutes ($SECONDS_UNTIL_RESET seconds)"
        echo "Reset time: $(date -d "@$RESET_TIME" 2>/dev/null || date -r $RESET_TIME 2>/dev/null || echo "Unix timestamp: $RESET_TIME")"
    else
        echo ""
        echo "Rate limit should be reset now!"
    fi
fi
