// MTGA Advisor — WebSocket client
let ws = null;
let reconnectTimer = null;
let currentState = null;
let currentAdvice = [];
let currentThreats = [];

function connect() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${location.host}/ws`);

    ws.onopen = () => {
        document.getElementById('connection-status').className = 'status-dot connected';
        if (reconnectTimer) {
            clearInterval(reconnectTimer);
            reconnectTimer = null;
        }
    };

    ws.onclose = () => {
        document.getElementById('connection-status').className = 'status-dot disconnected';
        if (!reconnectTimer) {
            reconnectTimer = setInterval(connect, 3000);
        }
    };

    ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        switch (msg.type) {
            case 'state_update':
                currentState = msg.data;
                renderState(msg.data);
                break;
            case 'advice':
                renderAdvice(msg.data);
                break;
            case 'backend_changed':
                document.getElementById('backend-select').value = msg.data.backend;
                break;
            case 'strategy_info':
                renderStrategyInfo(msg.data);
                break;
            case 'threat_assessment':
                console.log('Threat assessment received:', msg.data.length, 'threats');
                renderThreats(msg.data);
                break;
            case 'match_start':
                clearScreen();
                break;
            case 'decision_point':
                currentState = msg.data.state;
                renderState(msg.data.state);
                document.getElementById('advice-panel').classList.add('decision-active');
                setTimeout(() => {
                    document.getElementById('advice-panel').classList.remove('decision-active');
                }, 5000);
                break;
        }
    };
}

function renderState(state) {
    // Match status
    if (state.match_id) {
        const status = state.stage === 'GameStage_GameOver' ? 'Game Over' :
            state.stage === 'GameStage_Start' ? 'Starting...' : 'In Game';
        document.getElementById('match-status').textContent =
            `vs ${state.opponent_name || '?'} — ${status}`;
    } else {
        document.getElementById('match-status').textContent = 'Waiting for match...';
    }

    // Turn info
    const ti = state.turn;
    if (ti && ti.number > 0) {
        const whose = ti.is_my_turn ? 'Your turn' : "Opponent's turn";
        document.getElementById('turn-info').textContent =
            `T${ti.number} | ${ti.phase_display} | ${whose}`;
    } else {
        document.getElementById('turn-info').textContent = '';
    }

    // Life totals
    document.getElementById('opp-life').textContent = state.opp_life || 0;
    document.getElementById('my-life').textContent = state.my_life || 0;
    document.getElementById('opp-name').textContent = state.opponent_name || 'Opponent';

    // Mana info
    const lands = (state.my_battlefield || []).filter(c => c.is_land);
    const untapped = lands.filter(c => !c.is_tapped);
    document.getElementById('mana-info').textContent =
        `Mana: ${untapped.length}/${lands.length}`;

    // Battlefields
    renderCardRow('opp-battlefield', state.opp_battlefield || []);
    renderCardRow('my-battlefield', state.my_battlefield || []);
    renderCardRow('hand-cards', state.hand || [], true);

    // Highlights
    highlightCards();

    // Footer
    document.getElementById('library-count').textContent = `Library: ${state.my_library_count || 0}`;
    document.getElementById('graveyard-count').textContent =
        `Graveyard: ${state.my_graveyard_count || 0}`;
    const stackCount = state.stack_count || 0;
    document.getElementById('stack-count').textContent =
        stackCount > 0 ? `Stack: ${stackCount}` : '';
    document.getElementById('game-state-id').textContent =
        `State #${state.game_state_id || 0}`;
}

function renderCardRow(containerId, cards, isHand = false) {
    const container = document.getElementById(containerId);
    if (!cards.length) {
        container.innerHTML = '<span class="empty-state">Empty</span>';
        return;
    }

    container.innerHTML = cards.map(card => {
        const classes = ['card'];

        // Card type class
        const types = card.card_types || [];
        if (types.includes('Creature')) classes.push('creature');
        else if (types.includes('Land')) classes.push('land');
        else if (types.includes('Instant') || types.includes('Sorcery')) classes.push('spell');
        else if (types.includes('Enchantment')) classes.push('enchantment');
        else if (types.includes('Artifact')) classes.push('artifact');

        // Color class
        const colors = card.colors || [];
        if (colors.length > 1) classes.push('color-multi');
        else if (colors.length === 1) classes.push(`color-${colors[0]}`);

        // State classes
        if (card.is_tapped) classes.push('tapped');
        if (card.has_summoning_sickness) classes.push('summoning-sick');

        // Stats
        let stats = '';
        if (types.includes('Creature')) {
            stats = `${card.power}/${card.toughness}`;
        }

        // Abilities (show first keyword)
        const abilities = card.abilities || [];
        const keywords = abilities.filter(a => a.length < 20).slice(0, 2).join(', ');

        return `<div class="${classes.join(' ')}" data-name="${card.name}" data-iid="${card.instance_id || ''}" title="${card.name}\n${(card.abilities || []).join(', ')}">
            ${card.mana_cost ? `<span class="card-cost">${formatMana(card.mana_cost)}</span>` : ''}
            <div class="card-name">${card.name}</div>
            <div class="card-stats">${stats}${keywords ? ' · ' + keywords : ''}</div>
        </div>`;
    }).join('');
}

function formatMana(manaStr) {
    if (!manaStr) return '';
    // Convert oW, oU, oB, oR, oG, o1, o2 etc. to symbols
    return manaStr
        .replace(/o(\d+)/g, '$1')
        .replace(/oW/g, 'W')
        .replace(/oU/g, 'U')
        .replace(/oB/g, 'B')
        .replace(/oR/g, 'R')
        .replace(/oG/g, 'G')
        .replace(/oC/g, 'C')
        .replace(/oX/g, 'X');
}

function renderStrategyInfo(info) {
    // Strategy banner
    const el = document.getElementById('strategy-info');
    if (!info || !info.strategy_name) {
        el.style.display = 'none';
    } else {
        el.innerHTML = `Deck: <strong>${info.strategy_name}</strong> (${info.archetype}, ${info.rule_count} rules)`;
        el.style.display = 'block';
    }

    // Opponent meta badge
    const oppMeta = document.getElementById('opp-meta');
    const oppDetails = document.getElementById('opp-meta-details');
    if (info && info.opp_deck) {
        const conf = info.opp_confidence;
        const confClass = conf >= 80 ? 'high' : conf >= 50 ? 'mid' : 'low';
        oppMeta.innerHTML = `<span class="opp-deck-badge ${confClass}">${info.opp_deck} (${conf}%)</span>`;

        // Details row
        let parts = [];
        if (info.opp_archetype) parts.push(info.opp_archetype);
        if (info.opp_speed) parts.push(`speed: ${info.opp_speed}`);
        if (info.opp_kill_turn) parts.push(`kills T${info.opp_kill_turn}`);
        if (info.opp_hidden_reach) parts.push(`reach: ${info.opp_hidden_reach} dmg`);

        let threats = (info.opp_key_threats || []).map(t => typeof t === 'string' ? t : t.card || t).join(', ');
        if (threats) parts.push(`threats: ${threats}`);

        let seen = (info.opp_cards_seen || []).join(', ');
        if (seen) parts.push(`seen: ${seen}`);

        if (parts.length) {
            oppDetails.textContent = parts.join(' | ');
            oppDetails.style.display = 'block';
        }
    } else {
        oppMeta.innerHTML = '';
        if (info && info.opp_cards_seen && info.opp_cards_seen.length) {
            oppDetails.textContent = `Cards seen: ${info.opp_cards_seen.join(', ')}`;
            oppDetails.style.display = 'block';
        } else {
            oppDetails.style.display = 'none';
        }
    }
}

function renderThreats(threats) {
    const panel = document.getElementById('threat-panel');
    const container = document.getElementById('threat-list');
    const counter = document.getElementById('threat-count');

    if (!threats || !threats.length) {
        currentThreats = [];
        panel.style.display = 'none';
        highlightCards();
        return;
    }

    currentThreats = threats;
    panel.style.display = 'block';
    counter.textContent = threats.length;

    container.innerHTML = threats.map(t => {
        const danger = t.danger || 2;
        const dangerClass = `danger-${danger}`;
        const priorityClass = (t.priority || 'monitor').replace(/\s+/g, '-');
        const priorityLabel = (t.priority || 'monitor').replace('-', ' ');
        const analyzing = t.source === 'heuristic'
            ? '<span class="threat-analyzing">analyzing...</span>' : '';

        return `<div class="threat-item ${dangerClass}">
            <div class="threat-top">
                <span class="danger-badge ${dangerClass}">${danger}</span>
                <span class="threat-name">${t.name}</span>
                <span class="threat-cost">${formatMana(t.mana_cost || '')}</span>
            </div>
            <div class="threat-summary">${formatMessage(t.summary || '')}</div>
            <div class="threat-bottom">
                <span class="threat-priority ${priorityClass}">${priorityLabel}</span>
                <span class="threat-type">${t.type_line || ''}</span>
                ${analyzing}
            </div>
        </div>`;
    }).join('');

    highlightCards();
}

function highlightCards() {
    // Clear all existing highlights
    document.querySelectorAll('.card').forEach(el => {
        el.classList.remove('highlight-attack', 'highlight-block', 'highlight-cast',
                            'highlight-target', 'highlight-threat');
        const badge = el.querySelector('.action-badge');
        if (badge) badge.remove();
    });

    if (!currentState) return;

    // Collect known card names by zone
    const myBfNames = new Set((currentState.my_battlefield || []).map(c => c.name));
    const oppBfNames = new Set((currentState.opp_battlefield || []).map(c => c.name));
    const handNames = new Set((currentState.hand || []).map(c => c.name));

    // Parse advice to find mentioned cards and actions
    for (const a of currentAdvice) {
        const msg = (a.message || '').toLowerCase();

        // Attack advice
        if (msg.includes('attack with') || msg.includes('lethal')) {
            for (const name of myBfNames) {
                if (msg.toLowerCase().includes(name.toLowerCase())) {
                    addHighlight('my-battlefield', name, 'highlight-attack', 'ATK');
                }
            }
            // "attack with all" — highlight all untapped creatures
            if (msg.includes('attack with all') || msg.includes('lethal')) {
                (currentState.my_battlefield || []).forEach(c => {
                    if ((c.card_types || []).includes('Creature') && !c.is_tapped && !c.has_summoning_sickness) {
                        addHighlight('my-battlefield', c.name, 'highlight-attack', 'ATK');
                    }
                });
            }
        }

        // Block advice
        if (msg.includes('block') && !msg.includes("can't block")) {
            for (const name of myBfNames) {
                if (msg.toLowerCase().includes(name.toLowerCase()) && !msg.startsWith('block ' + name.toLowerCase())) {
                    addHighlight('my-battlefield', name, 'highlight-block', 'BLK');
                }
            }
            // Highlight the attacker being blocked on opponent side
            for (const name of oppBfNames) {
                if (msg.toLowerCase().includes(name.toLowerCase())) {
                    addHighlight('opp-battlefield', name, 'highlight-target', 'TGT');
                }
            }
        }

        // Removal advice
        if (msg.includes('remove') || msg.includes('destroy') || msg.includes('exile')) {
            for (const name of oppBfNames) {
                if (msg.toLowerCase().includes(name.toLowerCase())) {
                    addHighlight('opp-battlefield', name, 'highlight-target', 'TGT');
                }
            }
            // Highlight the removal spell in hand
            for (const name of handNames) {
                if (msg.toLowerCase().includes(name.toLowerCase())) {
                    addHighlight('hand-cards', name, 'highlight-cast', 'CAST');
                }
            }
        }

        // Cast advice
        if (msg.includes('cast ')) {
            for (const name of handNames) {
                if (msg.toLowerCase().includes(name.toLowerCase())) {
                    addHighlight('hand-cards', name, 'highlight-cast', 'CAST');
                }
            }
        }
    }

    // Highlight opponent permanents from threat panel (danger >= 4)
    for (const t of currentThreats) {
        if (t.danger >= 4) {
            addHighlight('opp-battlefield', t.name, 'highlight-threat',
                         t.priority === 'must-remove' ? 'KILL' : 'TGT');
        }
    }
}

function addHighlight(containerId, cardName, cssClass, badgeText) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.querySelectorAll('.card').forEach(el => {
        if (el.dataset.name === cardName && !el.classList.contains(cssClass)) {
            el.classList.add(cssClass);
            if (badgeText && !el.querySelector('.action-badge')) {
                const badge = document.createElement('span');
                badge.className = `action-badge badge-${cssClass.replace('highlight-', '')}`;
                badge.textContent = badgeText;
                el.appendChild(badge);
            }
        }
    });
}

function clearScreen() {
    currentState = null;
    currentAdvice = [];
    currentThreats = [];
    document.getElementById('match-status').textContent = 'New match starting...';
    document.getElementById('turn-info').textContent = '';
    document.getElementById('opp-life').textContent = '20';
    document.getElementById('my-life').textContent = '20';
    document.getElementById('opp-name').textContent = 'Opponent';
    document.getElementById('mana-info').textContent = '';
    document.getElementById('opp-battlefield').innerHTML = '<span class="empty-state">Empty</span>';
    document.getElementById('my-battlefield').innerHTML = '<span class="empty-state">Empty</span>';
    document.getElementById('hand-cards').innerHTML = '<span class="empty-state">Empty</span>';
    document.getElementById('advice-list').innerHTML = '<span class="empty-state">New match — good luck!</span>';
    document.getElementById('library-count').textContent = 'Library: 0';
    document.getElementById('graveyard-count').textContent = 'Graveyard: 0';
    document.getElementById('stack-count').textContent = '';
    document.getElementById('game-state-id').textContent = '';
    document.getElementById('strategy-info').style.display = 'none';
    document.getElementById('opp-meta').innerHTML = '';
    document.getElementById('opp-meta-details').style.display = 'none';
    document.getElementById('threat-panel').style.display = 'none';
    document.getElementById('threat-list').innerHTML = '';
}

function formatMessage(text) {
    if (!text) return '';
    return text
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\n/g, '<br>');
}

function resetAskButton() {
    const btn = document.getElementById('ask-ai-btn');
    if (btn) { btn.textContent = 'Ask AI'; btn.disabled = false; }
}

function renderAdvice(adviceList) {
    resetAskButton();
    resetSummaryButton();
    const container = document.getElementById('advice-list');
    if (!adviceList || !adviceList.length) {
        currentAdvice = [];
        container.innerHTML = '<span class="empty-state">No advice yet — play a game!</span>';
        return;
    }

    currentAdvice = adviceList;

    // Sort by priority
    const order = { critical: 0, high: 1, medium: 2, low: 3 };
    adviceList.sort((a, b) => (order[a.priority] || 3) - (order[b.priority] || 3));

    container.innerHTML = adviceList.map(a => `
        <div class="advice-item ${a.priority}">
            <div class="advice-message">${formatMessage(a.message)}</div>
            <span class="advice-source">[${a.source}]</span>
            ${a.details ? `<div class="advice-details">${a.details}</div>` : ''}
        </div>
    `).join('');

    highlightCards();
}

function askLLM() {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action: 'ask_llm' }));
        const btn = document.getElementById('ask-ai-btn');
        btn.textContent = 'Thinking...';
        btn.disabled = true;
        // Claude CLI can take up to 40s, reset after 45s
        setTimeout(() => {
            btn.textContent = 'Ask AI';
            btn.disabled = false;
        }, 45000);
    }
}

function matchSummary() {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action: 'match_summary' }));
        const btn = document.getElementById('summary-btn');
        btn.textContent = 'Generating...';
        btn.disabled = true;
        setTimeout(() => {
            btn.textContent = 'Match Summary';
            btn.disabled = false;
        }, 60000);
    }
}

function resetSummaryButton() {
    const btn = document.getElementById('summary-btn');
    if (btn) { btn.textContent = 'Match Summary'; btn.disabled = false; }
}

function toggleLLM(enabled) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action: 'toggle_llm', enabled }));
    }
}

function setBackend(backend) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action: 'set_backend', backend }));
    }
}

// Init
connect();
