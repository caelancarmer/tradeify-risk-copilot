// Small, focused script for Unified Ops Console
// Features: admin key handling, HTMX headers, approve/reject confirmations

document.addEventListener('DOMContentLoaded', function() {
    // Global admin key variable
    let adminKey = '';

    // Update admin key when input changes
    const adminKeyInput = document.getElementById('admin-key');
    if (adminKeyInput) {
        adminKeyInput.addEventListener('input', function(e) {
            adminKey = e.target.value;
        });
    }

    // Configure HTMX to include X-Admin-Key header for all requests
    if (typeof htmx !== 'undefined') {
        htmx.config.defaultHeaders = htmx.config.defaultHeaders || {};
        htmx.config.defaultHeaders['X-Admin-Key'] = ''; // Will be updated dynamically

        // Update headers before each request
        document.body.addEventListener('htmx:beforeRequest', function(event) {
            if (adminKey) {
                event.detail.headers = event.detail.headers || {};
                event.detail.headers['X-Admin-Key'] = adminKey;
            }
        });
    }

    // Auto-refresh time display
    function updateRefreshTime() {
        const now = new Date();
        const timeElement = document.getElementById('refresh-time');
        if (timeElement) {
            timeElement.textContent = `Last updated: ${now.toLocaleTimeString()}`;
        }
    }

    // Update refresh time every second
    setInterval(updateRefreshTime, 1000);
    updateRefreshTime();

    // Health badge animations and updates
    function updateHealthBadge(elementId, status, text) {
        const element = document.getElementById(elementId);
        if (!element) return;

        // Remove all status classes
        element.className = 'badge';

        // Add appropriate status class
        if (status === 'ok') {
            element.classList.add('badge-ok');
        } else if (status === 'degraded') {
            element.classList.add('badge-degraded');
        } else if (status === 'mock') {
            element.classList.add('badge-mock');
        } else {
            element.classList.add('badge-unreachable');
        }

        // Remove spinner if present
        const spinner = element.querySelector('.spinner');
        if (spinner) spinner.remove();

        // Set text
        element.innerHTML = text;
    }

    // Fetch dashboard health data and update badges
    function updateHealthBadges() {
        fetch('/ops')
            .then(response => response.json())
            .then(data => {
                updateHealthBadge('postgres-badge', data.health.postgres.status,
                    `PostgreSQL: ${data.health.postgres.status}`);
                updateHealthBadge('redis-badge', data.health.redis.status,
                    `Redis: ${data.health.redis.status}`);
                updateHealthBadge('router-badge', data.health.model_router.status,
                    `Model Router: ${data.health.model_router.status}`);
            })
            .catch(error => {
                console.error('Failed to fetch health data:', error);
                // Update badges to show error state
                updateHealthBadge('postgres-badge', 'unreachable', 'PostgreSQL: unreachable');
                updateHealthBadge('redis-badge', 'unreachable', 'Redis: unreachable');
                updateHealthBadge('router-badge', 'mock', 'Model Router: mock');
            });
    }

    // Initial health badge update
    updateHealthBadges();

    // Update health badges every 30 seconds
    setInterval(updateHealthBadges, 30000);

    // Handle approve/reject button clicks
    function attachActionHandlers() {
        document.querySelectorAll('.button-approve, .button-reject').forEach(button => {
            // Remove existing listeners to avoid duplicates
            button.replaceWith(button.cloneNode(true));
        });

        document.querySelectorAll('.button-approve, .button-reject').forEach(button => {
            button.addEventListener('click', function(e) {
                e.preventDefault();

                // Check if admin key is provided
                if (!adminKey) {
                    alert('Please enter X-Admin-Key in the header to perform admin actions');
                    return;
                }

                // Get request ID and action
                const requestId = this.getAttribute('data-request-id');
                const action = this.classList.contains('button-approve') ? 'approve' : 'reject';

                // Confirmation dialog
                const actionText = action === 'approve' ? 'approve' : 'reject';
                if (!confirm(`Are you sure you want to ${actionText} request ${requestId}?`)) {
                    return;
                }

                // Make the request
                const url = `/payout/${requestId}/${action}`;
                fetch(url, {
                    method: 'POST',
                    headers: {
                        'X-Admin-Key': adminKey,
                        'Content-Type': 'application/json'
                    }
                })
                .then(response => {
                    if (!response.ok) {
                        return response.text().then(text => {
                            throw new Error(`HTTP ${response.status}: ${text}`);
                        });
                    }
                    return response.json();
                })
                .then(data => {
                    // Show success feedback
                    const originalText = this.textContent;
                    this.textContent = '✓ Done';
                    this.style.background = action === 'approve' ? '#4ade80' : '#f87171';

                    setTimeout(() => {
                        this.textContent = originalText;
                        this.style.background = '';
                    }, 2000);

                    // Reload panels to reflect changes
                    reloadPanels();
                })
                .catch(error => {
                    console.error('Action failed:', error);
                    alert(`Failed to ${action} request: ${error.message}`);
                });
            });
        });
    }

    // Reload panels to get fresh data
    function reloadPanels() {
        const panels = [
            { selector: '#accounts-panel-content', endpoint: '/ops/partials/accounts' },
            { selector: '#queue-panel-content', endpoint: '/ops/partials/queue' },
            { selector: '#decisions-panel-content', endpoint: '/ops/partials/decisions' }
        ];

        panels.forEach(panel => {
            const element = document.querySelector(panel.selector);
            if (element) {
                fetch(panel.endpoint)
                    .then(response => response.text())
                    .then(html => {
                        element.innerHTML = html;
                        attachActionHandlers();
                        updateRefreshTime();
                    })
                    .catch(error => {
                        console.error(`Failed to reload ${panel.selector}:`, error);
                    });
            }
        });
    }

    // Initialize action handlers
    attachActionHandlers();

    // Update queue summary badges when decisions panel loads
    function updateQueueSummary() {
        const queueBadges = document.querySelectorAll('.queue-badge');
        const counts = { pending: 0, manual: 0, approved: 0, rejected: 0 };

        // Count statuses from table rows (simplified)
        document.querySelectorAll('#queue-panel-content .status-badge').forEach(badge => {
            if (badge.classList.contains('status-pending')) counts.pending++;
            else if (badge.classList.contains('status-manual')) counts.manual++;
            else if (badge.classList.contains('status-approved')) counts.approved++;
            else if (badge.classList.contains('status-rejected')) counts.rejected++;
        });

        // Update badge text
        queueBadges.forEach(badge => {
            if (badge.classList.contains('queue-pending')) {
                badge.textContent = `PENDING: ${counts.pending}`;
            } else if (badge.classList.contains('queue-manual')) {
                badge.textContent = `MANUAL: ${counts.manual}`;
            } else if (badge.classList.contains('queue-approved')) {
                badge.textContent = `APPROVED: ${counts.approved}`;
            } else if (badge.classList.contains('queue-rejected')) {
                badge.textContent = `REJECTED: ${counts.rejected}`;
            }
        });

        // Update risk count
        const riskCount = document.getElementById('risk-count');
        if (riskCount) {
            riskCount.textContent = counts.manual; // Risk count is based on manual review requests
        }
    }

    // Call updateQueueSummary when queue panel loads
    document.addEventListener('htmx:afterRequest', function(evt) {
        if (evt.detail.elt && evt.detail.elt.closest('#queue-panel-content')) {
            setTimeout(updateQueueSummary, 100);
        }
    });
});