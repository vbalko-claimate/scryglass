// MTGA Advisor — WebSocket client
let ws = null;
let reconnectTimer = null;
let currentState = null;
let currentAdvice = [];
let currentThreats = [];
let currentStrategyInfo = null;
let currentLlmStatus = null;

// ─── Profile System ───
const PROFILES = ['focus', 'full', 'tactical'];
const PROFILE_MAX_SUPPORT = { focus: 2, full: 3, tactical: 5 };
let currentProfile = localStorage.getItem('scry-profile') || 'full';

function setProfile(profile) {
    if (!PROFILES.includes(profile)) return;
    currentProfile = profile;
    localStorage.setItem('scry-profile', profile);
    document.documentElement.className = 'profile-' + profile;
    document.querySelectorAll('.profile-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.profile === profile);
    });

    // Move vital bar into/out of header based on profile
    const vitalBar = document.getElementById('vital-bar');
    const header = document.querySelector('header');
    const mainLayout = document.getElementById('main-layout');
    if (vitalBar && header && mainLayout) {
        if (profile === 'focus' || profile === 'tactical') {
            // Move vital bar inside header
            header.appendChild(vitalBar);
        } else {
            // Move vital bar back before board-column inside main layout
            mainLayout.insertBefore(vitalBar, mainLayout.firstChild);
        }
    }

    syncVitalBar();
    if (currentAdvice.length) renderAdvice(currentAdvice);
}

function syncVitalBar() {
    if (!currentState) return;
    const el = (id) => document.getElementById(id);
    const vbMy = el('vb-my-life');
    const vbMana = el('vb-mana');
    const vbOpp = el('vb-opp-life');
    const vbMeta = el('vb-opp-meta');
    if (vbMy) vbMy.textContent = currentState.my_life ?? 20;
    if (vbOpp) vbOpp.textContent = currentState.opp_life ?? 20;
    if (vbMana) {
        const manaEl = el('mana-info');
        if (manaEl) vbMana.textContent = manaEl.textContent;
    }
    if (vbMeta) {
        const metaEl = el('opp-meta');
        if (metaEl) vbMeta.innerHTML = metaEl.innerHTML;
    }
}

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
            case 'llm_auto_changed':
                document.getElementById('auto-llm-toggle').checked = !!msg.data.enabled;
                document.getElementById('llm-mode-label').textContent =
                    msg.data.mode ? `(${msg.data.mode}${msg.data.scope ? `, ${msg.data.scope}` : ''})` : '';
                break;
            case 'strategy_info':
                currentStrategyInfo = msg.data || null;
                renderStrategyInfo(msg.data);
                break;
            case 'llm_status':
                currentLlmStatus = msg.data || null;
                renderLLMStatus(currentLlmStatus);
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
    updateAdviceSubtitle();
    syncVitalBar();
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

        const parts = [];
        if (info.opp_archetype) parts.push(info.opp_archetype);
        if (info.opp_speed) parts.push(`speed: ${info.opp_speed}`);
        if (info.opp_kill_turn) parts.push(`kills T${info.opp_kill_turn}`);
        if (info.opp_hidden_reach) parts.push(`reach: ${info.opp_hidden_reach} dmg`);

        const keyThreats = (info.opp_key_threats || []).map(t => {
            if (typeof t === 'string') return { card: t, reason: '' };
            return t || {};
        });
        const mustAnswer = keyThreats
            .filter(t => t.must_answer || t.removal_priority === 1)
            .slice(0, 3)
            .map(t => `<strong>${t.card}</strong>${t.reason ? `: ${t.reason}` : ''}`);
        const seen = (info.opp_cards_seen || []).join(', ');

        const lines = [];
        if (parts.length) lines.push(parts.join(' | '));
        if (info.opp_plan) lines.push(`<span class="opp-plan-label">plan:</span> ${info.opp_plan}`);
        if (mustAnswer.length) {
            lines.push(`<span class="opp-plan-label">must answer:</span> ${mustAnswer.join(' | ')}`);
        }
        if (seen) lines.push(`<span class="opp-plan-label">seen:</span> ${seen}`);

        if (lines.length) {
            oppDetails.innerHTML = lines.join('<br>');
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

    renderThreatRadar(currentThreats, info);

    // Sync vital bar opp meta
    const vbOppMeta = document.getElementById('vb-opp-meta');
    if (vbOppMeta) {
        const metaEl = document.getElementById('opp-meta');
        if (metaEl) vbOppMeta.innerHTML = metaEl.innerHTML;
    }
}

function buildThreatRadarItems(threats, info) {
    const live = (threats || [])
        .sort((a, b) => {
            const aScore = (a.must_answer ? 100 : 0) + (a.category === 'engine' ? 20 : 0) + (a.danger || 0);
            const bScore = (b.must_answer ? 100 : 0) + (b.category === 'engine' ? 20 : 0) + (b.danger || 0);
            return bScore - aScore;
        });
    const liveNames = new Set(live.map(t => t.name));
    const watch = ((info && info.opp_key_threats) || [])
        .map(item => typeof item === 'string' ? { card: item } : (item || {}))
        .filter(item => item.card && !liveNames.has(item.card));
    return { live, watch };
}

function renderThreatRadar(threats, info) {
    const panel = document.getElementById('threat-radar-panel');
    const summary = document.getElementById('threat-radar-summary');
    const container = document.getElementById('threat-radar-list');
    const counter = document.getElementById('threat-radar-count');
    if (!panel || !summary || !container || !counter) return;

    const { live, watch } = buildThreatRadarItems(threats, info);
    const plan = info && info.opp_plan ? info.opp_plan : '';
    const seen = (info && info.opp_cards_seen && info.opp_cards_seen.length)
        ? info.opp_cards_seen.join(', ')
        : '';

    if (!live.length && !watch.length && !plan && !seen) {
        panel.style.display = 'none';
        summary.innerHTML = '';
        container.innerHTML = '';
        counter.textContent = '';
        return;
    }

    panel.style.display = 'block';
    counter.textContent = `${live.length} live${watch.length ? ` · ${watch.length} watch` : ''}`;

    const summaryBits = [];
    if (plan) summaryBits.push(`<div><span class="threat-radar-label">Their plan:</span> ${formatMessage(plan)}</div>`);
    if (seen) summaryBits.push(`<div><span class="threat-radar-label">Seen:</span> ${seen}</div>`);
    summary.innerHTML = summaryBits.join('');

    const liveItems = live.map(t => {
        const danger = t.danger || 2;
        const dangerClass = `danger-${danger}`;
        const labels = [];
        if (t.role) labels.push(t.role.replace('-', ' '));
        if (t.category === 'engine') labels.push('engine');
        if (t.must_answer || t.priority === 'must-remove') labels.push('must-answer');
        else if ((t.priority || '') && t.priority !== 'monitor') labels.push((t.priority || '').replace('-', ' '));
        else labels.push('live');
        const typeLine = t.type_line ? `<span class="radar-type">${t.type_line}</span>` : '';
        const manaCost = t.mana_cost ? `<span class="radar-cost">${formatMana(t.mana_cost)}</span>` : '';
        const hint = t.decision_hint ? `<div class="radar-hint">${formatMessage(t.decision_hint)}</div>` : '';
        const reason = t.reason && t.reason !== t.summary
            ? `<div class="radar-reason">${formatMessage(t.reason)}</div>` : '';
        const analyzing = t.source === 'heuristic'
            ? '<span class="radar-analyzing">analyzing...</span>' : '';
        return `<div class="radar-item live ${dangerClass}">
            <div class="radar-top">
                <span class="danger-badge ${dangerClass}">${danger}</span>
                <span class="radar-name">${t.name}</span>
                ${manaCost}
            </div>
            <div class="radar-tags">${labels.map(label => `<span class="radar-tag">${label}</span>`).join('')}</div>
            <div class="radar-summary">${formatMessage(t.summary || t.reason || '')}</div>
            ${hint}
            ${reason}
            <div class="radar-bottom">
                ${typeLine}
                ${analyzing}
            </div>
        </div>`;
    });

    const watchItems = watch.map(t => `<div class="radar-item watch">
        <div class="radar-top">
            <span class="radar-watch-badge">?</span>
            <span class="radar-name">${t.card}</span>
        </div>
        <div class="radar-tags"><span class="radar-tag">watch for</span></div>
        <div class="radar-summary">${formatMessage(t.reason || 'Important payoff or enabler for this archetype.')}</div>
    </div>`);

    container.innerHTML = [...liveItems, ...watchItems].join('');
}

function renderThreats(threats) {
    if (!threats || !threats.length) {
        currentThreats = [];
        renderThreatRadar(currentThreats, currentStrategyInfo);
        highlightCards();
        return;
    }

    currentThreats = threats;
    renderThreatRadar(currentThreats, currentStrategyInfo);
    highlightCards();
}

// A2: Map action family to highlight CSS class and badge text
const FAMILY_HIGHLIGHT = {
    cast_spell: { css: 'highlight-cast',   badge: 'CAST' },
    play_land:  { css: 'highlight-cast',   badge: 'LAND' },
    attack:     { css: 'highlight-attack',  badge: 'ATK'  },
    block:      { css: 'highlight-block',   badge: 'BLK'  },
    activate:   { css: 'highlight-cast',   badge: 'ACT'  },
    pass:       { css: 'highlight-cast',   badge: 'HOLD' },
};

function highlightCards() {
    // Clear all existing highlights
    document.querySelectorAll('.card').forEach(el => {
        el.classList.remove('highlight-attack', 'highlight-block', 'highlight-cast',
                            'highlight-target', 'highlight-threat');
        const badge = el.querySelector('.action-badge');
        if (badge) badge.remove();
    });

    if (!currentState) return;

    // A2: Check if any advice has structured action_scores
    const hasStructuredScores = currentAdvice.some(a =>
        a.action_scores && a.action_scores.length > 0);

    if (hasStructuredScores) {
        // Structured path: use action_scores.target to find cards by data-name
        for (const a of currentAdvice) {
            const scores = a.action_scores || [];
            for (const actionScore of scores) {
                const target = actionScore.target;
                if (!target) continue;
                const family = actionScore.family || 'cast_spell';
                const mapping = FAMILY_HIGHLIGHT[family] || FAMILY_HIGHLIGHT.cast_spell;

                // Try hand first (cast_spell, play_land, activate)
                if (family === 'cast_spell' || family === 'play_land' || family === 'activate') {
                    addHighlight('hand-cards', target, mapping.css, mapping.badge);
                }
                // Attack: highlight on my battlefield
                if (family === 'attack') {
                    addHighlight('my-battlefield', target, mapping.css, mapping.badge);
                }
                // Block: highlight blocker on my battlefield
                if (family === 'block') {
                    addHighlight('my-battlefield', target, mapping.css, mapping.badge);
                }
                // Also try my battlefield for cast targets already in play (auras, equipment)
                if (family === 'cast_spell' || family === 'activate') {
                    addHighlight('my-battlefield', target, mapping.css, mapping.badge);
                }
            }
        }
    } else {
        // Fallback: legacy regex-based highlighting for backward compat
        const myBfNames = new Set((currentState.my_battlefield || []).map(c => c.name));
        const oppBfNames = new Set((currentState.opp_battlefield || []).map(c => c.name));
        const handNames = new Set((currentState.hand || []).map(c => c.name));

        for (const a of currentAdvice) {
            const msg = (a.message || '').toLowerCase();

            if (msg.includes('attack with') || msg.includes('lethal')) {
                for (const name of myBfNames) {
                    if (msg.toLowerCase().includes(name.toLowerCase())) {
                        addHighlight('my-battlefield', name, 'highlight-attack', 'ATK');
                    }
                }
                if (msg.includes('attack with all') || msg.includes('lethal')) {
                    (currentState.my_battlefield || []).forEach(c => {
                        if ((c.card_types || []).includes('Creature') && !c.is_tapped && !c.has_summoning_sickness) {
                            addHighlight('my-battlefield', c.name, 'highlight-attack', 'ATK');
                        }
                    });
                }
            }

            if (msg.includes('block') && !msg.includes("can't block")) {
                for (const name of myBfNames) {
                    if (msg.toLowerCase().includes(name.toLowerCase()) && !msg.startsWith('block ' + name.toLowerCase())) {
                        addHighlight('my-battlefield', name, 'highlight-block', 'BLK');
                    }
                }
                for (const name of oppBfNames) {
                    if (msg.toLowerCase().includes(name.toLowerCase())) {
                        addHighlight('opp-battlefield', name, 'highlight-target', 'TGT');
                    }
                }
            }

            if (msg.includes('remove') || msg.includes('destroy') || msg.includes('exile')) {
                for (const name of oppBfNames) {
                    if (msg.toLowerCase().includes(name.toLowerCase())) {
                        addHighlight('opp-battlefield', name, 'highlight-target', 'TGT');
                    }
                }
                for (const name of handNames) {
                    if (msg.toLowerCase().includes(name.toLowerCase())) {
                        addHighlight('hand-cards', name, 'highlight-cast', 'CAST');
                    }
                }
            }

            if (msg.includes('cast ')) {
                for (const name of handNames) {
                    if (msg.toLowerCase().includes(name.toLowerCase())) {
                        addHighlight('hand-cards', name, 'highlight-cast', 'CAST');
                    }
                }
            }
        }
    }

    // Highlight opponent permanents from threat panel (danger >= 4) — always active
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
    currentStrategyInfo = null;
    currentLlmStatus = null;
    document.getElementById('match-status').textContent = 'New match starting...';
    document.getElementById('turn-info').textContent = '';
    document.getElementById('opp-life').textContent = '20';
    document.getElementById('my-life').textContent = '20';
    document.getElementById('opp-name').textContent = 'Opponent';
    document.getElementById('mana-info').textContent = '';
    document.getElementById('opp-battlefield').innerHTML = '<span class="empty-state">Empty</span>';
    document.getElementById('my-battlefield').innerHTML = '<span class="empty-state">Empty</span>';
    document.getElementById('hand-cards').innerHTML = '<span class="empty-state">Empty</span>';
    document.getElementById('decision-actions-summary').innerHTML = '';
    document.getElementById('advice-key-play').style.display = 'none';
    document.getElementById('advice-key-play').innerHTML = '';
    document.getElementById('advice-now-list').innerHTML = '<span class="empty-state">New match — good luck!</span>';
    document.getElementById('advice-context-list').innerHTML = '<span class="empty-state">Opponent intel and AI notes will appear here.</span>';
    document.getElementById('library-count').textContent = 'Library: 0';
    document.getElementById('graveyard-count').textContent = 'Graveyard: 0';
    document.getElementById('stack-count').textContent = '';
    document.getElementById('game-state-id').textContent = '';
    document.getElementById('strategy-info').style.display = 'none';
    document.getElementById('opp-meta').innerHTML = '';
    document.getElementById('opp-meta-details').style.display = 'none';
    document.getElementById('threat-radar-panel').style.display = 'none';
    document.getElementById('threat-radar-summary').innerHTML = '';
    document.getElementById('threat-radar-list').innerHTML = '';
    document.getElementById('threat-radar-count').textContent = '';
    updateAdviceSubtitle();
    renderLLMStatus({ state: 'idle', label: 'LLM idle', source: 'auto', wait: false });
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

function updateAdviceSubtitle() {
    const el = document.getElementById('advice-subtitle');
    if (!el) return;

    const status = currentLlmStatus || { state: 'idle' };
    if (!currentState || !currentState.turn || !currentState.turn.number) {
        el.textContent = status.state === 'pending'
            ? 'Live state loading. LLM supplement is already spinning up.'
            : 'Heuristics pilot the turn. LLM supplements when needed.';
        return;
    }

    const turn = currentState.turn;
    const owner = turn.is_my_turn ? 'Your turn' : "Opponent's turn";
    const phase = turn.phase_display || 'Unknown phase';
    let text = `${owner} · ${phase}`;
    if (status.state === 'pending') {
        text += ' · LLM supplement incoming';
    } else if (status.state === 'done') {
        text += ' · LLM note landed';
    }
    el.textContent = text;
}

function renderLLMStatus(status) {
    const pill = document.getElementById('llm-status-pill');
    const panel = document.getElementById('advice-panel');
    const safe = status || { state: 'idle', label: 'LLM idle', source: 'auto', wait: false };
    const state = safe.state || 'idle';
    const source = safe.source || 'auto';
    const turn = safe.turn_number ? ` · T${safe.turn_number}` : '';
    const phase = safe.phase_display ? ` · ${safe.phase_display}` : '';

    if (panel) {
        panel.classList.toggle('llm-pending', state === 'pending');
    }
    updateAdviceSubtitle();
    if (!pill) return;

    pill.className = `llm-status-pill ${state}`;
    pill.textContent = `${safe.label || 'LLM idle'}${turn}${phase}`;
    pill.title = safe.wait
        ? 'LLM supplement is still running for the current spot.'
        : 'No pending LLM work for the current spot.';

    if (state !== 'pending') {
        resetAskButton();
    }
    if (state === 'pending' && source === 'manual') {
        const btn = document.getElementById('ask-ai-btn');
        if (btn) {
            btn.textContent = 'Thinking...';
            btn.disabled = true;
        }
    }
}

function isContextAdvice(item) {
    const source = (item.source || '').toLowerCase();
    const msg = (item.message || '').toLowerCase();
    if (source === 'intel' || source.startsWith('llm')) return true;
    return (
        msg.startsWith('their plan:') ||
        msg.startsWith('must answer') ||
        msg.startsWith('watch for') ||
        msg.startsWith('engine online:') ||
        msg.startsWith('primary threat:')
    );
}

// A1: Family label map for action badges
const ACTION_FAMILY_LABELS = {
    cast_spell: 'CAST',
    play_land:  'LAND',
    attack:     'ATK',
    block:      'BLK',
    activate:   'ACT',
    pass:       'HOLD',
};

function renderAdviceItems(items, emptyText) {
    if (!items.length) {
        return `<span class="empty-state">${emptyText}</span>`;
    }

    return items.map(a => {
        // A1: Build action badge from first action_score family
        let badgeHtml = '';
        const scores = a.action_scores || [];
        if (scores.length) {
            const topFamily = scores.reduce((best, cur) =>
                cur.score > best.score ? cur : best, scores[0]).family;
            const label = ACTION_FAMILY_LABELS[topFamily] || topFamily.toUpperCase();
            badgeHtml = `<span class="advice-action-badge action-${topFamily}">${label}</span>`;
        }

        // A5: Rule provenance tooltip
        let titleAttr = '';
        const ruledScores = scores.filter(s => s.rule_id);
        if (ruledScores.length) {
            const tips = ruledScores.map(s =>
                `[${s.rule_layer || '?'}] ${s.rule_id} w:${s.rule_weight ?? '?'}`);
            titleAttr = ` title="${tips.join(' | ').replace(/"/g, '&quot;')}"`;
        }

        return `
        <div class="advice-item ${a.priority}"${titleAttr}>
            <div class="advice-message">${badgeHtml}${formatMessage(a.message)}</div>
            <span class="advice-source">[${a.source}]</span>
            ${a.details ? `<div class="advice-details">${a.details}</div>` : ''}
        </div>
    `;
    }).join('');
}

function spotlightScore(item) {
    const priorityScore = { critical: 400, high: 300, medium: 200, low: 100 };
    let score = priorityScore[item.priority] || 0;

    // A3: If action_scores exist, use max score from them as the primary signal
    const actionScores = item.action_scores || [];
    if (actionScores.length) {
        const maxActionScore = Math.max(...actionScores.map(a => a.score));
        // Scale 0-1 action score into 0-200 range, added on top of priority
        score += maxActionScore * 200;
        // Prefer specific advice (names a card) over generic ("spend all mana")
        const hasTarget = actionScores.some(a => a.target && a.target.length > 0);
        if (hasTarget) score += 50;
        return score;
    }

    // Fallback: existing text heuristic when no action_scores
    const source = (item.source || '').toLowerCase();
    const msg = (item.message || '').toLowerCase();

    if (source === 'heuristic') score += 40;
    else if (source === 'strategy') score += 25;
    else if (source.startsWith('llm')) score += 10;

    if (/^(remove|must block|must answer|attack|don't attack|block|cast|hold)/.test(msg)) {
        score += 25;
    }
    if (msg.startsWith('play a land')) score -= 50;
    if (msg.startsWith('their plan:') || msg.startsWith('primary threat:')) score -= 100;

    return score;
}

function renderSpotlightAdvice(item) {
    if (!item) return '';

    // A1: Badge for spotlight
    let badgeHtml = '';
    const scores = item.action_scores || [];
    if (scores.length) {
        const topFamily = scores.reduce((best, cur) =>
            cur.score > best.score ? cur : best, scores[0]).family;
        const label = ACTION_FAMILY_LABELS[topFamily] || topFamily.toUpperCase();
        badgeHtml = `<span class="advice-action-badge action-${topFamily}">${label}</span> `;
    }

    // A5: Rule provenance tooltip on spotlight
    let titleAttr = '';
    const ruledScores = scores.filter(s => s.rule_id);
    if (ruledScores.length) {
        const tips = ruledScores.map(s =>
            `[${s.rule_layer || '?'}] ${s.rule_id} w:${s.rule_weight ?? '?'}`);
        titleAttr = ` title="${tips.join(' | ').replace(/"/g, '&quot;')}"`;
    }

    return `
        <div class="advice-spotlight-card ${item.priority}"${titleAttr}>
            <div class="advice-spotlight-label">${badgeHtml}Key Play</div>
            <div class="advice-spotlight-message">${formatMessage(item.message)}</div>
            <div class="advice-spotlight-meta">
                <span class="advice-spotlight-source">${item.source}</span>
                ${item.details ? `<span class="advice-spotlight-details">${item.details}</span>` : ''}
            </div>
        </div>
    `;
}

function renderDecisionSummary(adviceList) {
    const el = document.getElementById('decision-actions-summary');
    if (!el) return;

    if (!adviceList || !adviceList.length) {
        el.innerHTML = '';
        return;
    }

    // Collect all action families with their best score
    const familyBest = {};
    for (const a of adviceList) {
        for (const s of (a.action_scores || [])) {
            if (!familyBest[s.family] || s.score > familyBest[s.family]) {
                familyBest[s.family] = s.score;
            }
        }
    }

    const families = Object.entries(familyBest);
    if (!families.length) {
        el.innerHTML = '';
        return;
    }

    // Sort by score descending
    families.sort((a, b) => b[1] - a[1]);
    const badges = families.map(([family]) => {
        const label = ACTION_FAMILY_LABELS[family] || family.toUpperCase();
        return `<span class="summary-family advice-action-badge action-${family}">${label}</span>`;
    });
    el.innerHTML = `Choose: ${badges.join(' ')}`;
}

function renderAdvice(adviceList) {
    resetAskButton();
    resetSummaryButton();
    const spotlightContainer = document.getElementById('advice-key-play');
    const nowContainer = document.getElementById('advice-now-list');
    const contextContainer = document.getElementById('advice-context-list');
    if (!spotlightContainer || !nowContainer || !contextContainer) return;

    // A4: Update decision action summary
    renderDecisionSummary(adviceList);

    if (!adviceList || !adviceList.length) {
        currentAdvice = [];
        spotlightContainer.style.display = 'none';
        spotlightContainer.innerHTML = '';
        nowContainer.innerHTML = '<span class="empty-state">No advice yet — play a game!</span>';
        contextContainer.innerHTML = '<span class="empty-state">Opponent intel and AI notes will appear here.</span>';
        return;
    }

    const order = { critical: 0, high: 1, medium: 2, low: 3 };
    const sortedAdvice = [...adviceList].sort(
        (a, b) => (order[a.priority] || 3) - (order[b.priority] || 3)
    );
    currentAdvice = sortedAdvice;

    let nowItems = sortedAdvice.filter(a => !isContextAdvice(a));
    let contextItems = sortedAdvice.filter(a => isContextAdvice(a));

    if (!nowItems.length && sortedAdvice.length) {
        const fallback = sortedAdvice.find(a => (a.source || '').toLowerCase() !== 'intel') || sortedAdvice[0];
        nowItems = [fallback];
        contextItems = sortedAdvice.filter(a => a !== fallback);
    }

    const spotlight = [...nowItems].sort((a, b) => spotlightScore(b) - spotlightScore(a))[0] || null;
    const supportItems = spotlight ? nowItems.filter(a => a !== spotlight) : nowItems;

    if (spotlight) {
        spotlightContainer.style.display = '';
        spotlightContainer.innerHTML = renderSpotlightAdvice(spotlight);
    } else {
        spotlightContainer.style.display = 'none';
        spotlightContainer.innerHTML = '';
    }

    nowItems = supportItems.slice(0, PROFILE_MAX_SUPPORT[currentProfile] || 3);
    contextItems = contextItems.slice(0, 5);

    nowContainer.innerHTML = renderAdviceItems(
        nowItems,
        spotlight ? 'No secondary actions for this spot.' : 'Waiting for a concrete play recommendation.'
    );
    contextContainer.innerHTML = renderAdviceItems(
        contextItems,
        'No extra context for this spot.'
    );

    highlightCards();
}

function askLLM() {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action: 'ask_llm' }));
        const btn = document.getElementById('ask-ai-btn');
        btn.textContent = 'Thinking...';
        btn.disabled = true;
        renderLLMStatus({
            state: 'pending',
            label: 'LLM thinking...',
            source: 'manual',
            wait: true,
        });
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

function resetReportButton() {
    const btn = document.getElementById('report-btn');
    if (btn) { btn.textContent = 'Export Last Game'; btn.disabled = false; }
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

async function exportLastGame() {
    const btn = document.getElementById('report-btn');
    if (!btn) return;
    btn.textContent = 'Exporting...';
    btn.disabled = true;

    try {
        const resp = await fetch('/api/match-report/latest', { cache: 'no-store' });
        if (!resp.ok) {
            throw new Error(`HTTP ${resp.status}`);
        }

        const text = await resp.text();
        const disposition = resp.headers.get('Content-Disposition') || '';
        const match = disposition.match(/filename=\"([^\"]+)\"/);
        const filename = match ? match[1] : 'mtga-match-report.md';
        const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
    } catch (err) {
        console.error('Failed to export report', err);
        alert('Failed to export the latest completed game report.');
    } finally {
        resetReportButton();
    }
}

// ─── Profile Switcher Init ───
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.profile-btn').forEach(btn => {
        btn.addEventListener('click', () => setProfile(btn.dataset.profile));
    });
    setProfile(currentProfile);
});

document.addEventListener('keydown', (e) => {
    if (e.target.matches('input, textarea, select')) return;
    if (e.ctrlKey && !e.shiftKey && !e.altKey) {
        if (e.key === '1') { e.preventDefault(); setProfile('focus'); }
        if (e.key === '2') { e.preventDefault(); setProfile('full'); }
        if (e.key === '3') { e.preventDefault(); setProfile('tactical'); }
    }
});

// Init
connect();
