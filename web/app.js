// Global State Management
var currentFleet = [];
var activeTaskId = null;
var pollInterval = null;
var selectedAuditDb = null;
var currentChatSessions = [];
var activeSessionId = 'session_default';
var currentUserProfile = {
  name: "Cloud DevOps Lead",
  role: "Database Administrator",
  avatar: "👨‍💻"
};

document.addEventListener('DOMContentLoaded', () => {
  initTabs();
  initControls();
  fetchConfig();
  fetchFleet();
  fetchUpgradeTasks();
  startTaskPolling();
  initChat();
  initDoomsday();

  // Auto refresh fleet status every 12 seconds
  setInterval(fetchFleet, 12000);
});

// Tab Switching
function initTabs() {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      const targetPane = document.getElementById(btn.dataset.tab);
      if (targetPane) targetPane.classList.add('active');
    });
  });
}

function switchTab(tabId) {
  const btn = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
  if (btn) btn.click();
}

// Config & Controls
async function fetchConfig() {
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    updateConfigUI(data);
  } catch (e) {
    console.error('Error fetching config:', e);
  }
}

function updateConfigUI(cfg) {
  const regionSelect = document.getElementById('regionSelect');
  const providerSelect = document.getElementById('llmProviderSelect');

  if (regionSelect) regionSelect.value = cfg.region;
  if (providerSelect) providerSelect.value = cfg.llm_provider;
}

function initControls() {
  // Region Change
  document.getElementById('regionSelect').addEventListener('change', async (e) => {
    await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ region: e.target.value })
    });
    fetchFleet();
  });

  // Model Provider Change
  document.getElementById('llmProviderSelect').addEventListener('change', async (e) => {
    await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ llm_provider: e.target.value })
    });
  });

  // Refresh Fleet
  document.getElementById('refreshFleetBtn').addEventListener('click', fetchFleet);

  // Settings Modal
  document.getElementById('settingsBtn').addEventListener('click', () => {
    openModal('settingsModal');
  });

  document.getElementById('saveSettingsBtn').addEventListener('click', async () => {
    const provider = document.getElementById('modalProviderSelect').value;
    const geminiKey = document.getElementById('geminiApiKeyInput').value;
    const ollamaEndpoint = document.getElementById('ollamaEndpointInput').value;

    await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        llm_provider: provider,
        gemini_api_key: geminiKey,
        local_llm_endpoint: ollamaEndpoint
      })
    });
    closeModal('settingsModal');
    fetchConfig();
    fetchFleet();
  });

  // Master Sidebar Search & Filter
  const searchInput = document.getElementById('masterSearchInput');
  if (searchInput) {
    searchInput.addEventListener('input', (e) => {
      masterSearchQuery = e.target.value.toLowerCase().trim();
      renderMasterDeployments(allTasks);
    });
  }

  const filterChips = document.getElementById('masterFilterChips');
  if (filterChips) {
    filterChips.addEventListener('click', (e) => {
      const chip = e.target.closest('[data-filter]');
      if (chip) {
        document.querySelectorAll('#masterFilterChips .stage-nav-pill').forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
        masterFilterCategory = chip.dataset.filter;
        renderMasterDeployments(allTasks);
      }
    });
  }
}

// Modal Helpers
function openModal(id) {
  document.getElementById(id).classList.add('open');
}

function closeModal(id) {
  document.getElementById(id).classList.remove('open');
}

// Fleet Discovery
async function fetchFleet() {
  const container = document.getElementById('fleetCardsGrid');
  container.innerHTML = '<div style="color: var(--text-muted); font-size: 0.95rem;">Scanning AWS region for databases...</div>';

  try {
    const res = await fetch('/api/databases');
    const data = await res.json();
    currentFleet = data.databases || [];
    renderFleet(currentFleet);
    updateDoomsdayDropdown(currentFleet);
    updateChatDbDropdown(currentFleet);
  } catch (e) {
    container.innerHTML = `<div style="color: var(--accent-rose);">Error discovering databases: ${e.message}</div>`;
  }
}

let allTasks = [];

function renderFleet(databases) {
  const container = document.getElementById('fleetCardsGrid');
  const badge = document.getElementById('fleetCountBadge');
  badge.textContent = databases.length;

  if (databases.length === 0) {
    container.innerHTML = '<div style="color: var(--text-muted);">No databases found in this region.</div>';
    return;
  }

  container.innerHTML = databases.map(db => {
    // Check if an active upgrade task is in-flight (not completed/failed)
    const activeTask = allTasks.find(t => (t.db_identifier === db.id || t.bg_id === (db.bg_deployment && db.bg_deployment.bg_id)) && t.status !== 'COMPLETED' && t.status !== 'FAILED');
    const isUpgrading = db.has_active_bg || !!activeTask;
    const isCompleted = db.has_completed_bg || (db.status && db.status.includes('Upgraded'));
    const isOldInstance = db.is_old_instance || db.id.includes('-old');
    const isBusy = isUpgrading || (db.aws_status && db.aws_status !== 'available' && !isOldInstance);
    const taskId = (db.bg_deployment && db.bg_deployment.bg_id) ? db.bg_deployment.bg_id : (activeTask ? activeTask.task_id : db.id);

    return `
    <div class="card" style="${isOldInstance ? 'border-color: rgba(239, 68, 68, 0.3); background: rgba(239, 68, 68, 0.03);' : (isCompleted ? 'border-color: rgba(16, 185, 129, 0.4); box-shadow: 0 0 16px rgba(16, 185, 129, 0.1);' : (isUpgrading ? 'border-color: rgba(245, 158, 11, 0.5); box-shadow: 0 0 16px rgba(245, 158, 11, 0.1);' : ''))}">
      <div class="card-header">
        <div>
          <div class="card-title" style="display: flex; align-items: center; gap: 8px;">
            ${escapeHtml(db.id)}
            ${isOldInstance ? '<span style="font-size: 0.68rem; background: rgba(239, 68, 68, 0.2); color: #f87171; padding: 2px 6px; border-radius: 4px; font-weight: 600;">OLD BLUE DB</span>' : ''}
          </div>
          <div style="font-size: 0.8rem; color: var(--text-faint); margin-top: 2px;">${escapeHtml(db.arn || '')}</div>
        </div>
        <span class="card-type-tag ${db.is_aurora ? 'tag-aurora' : 'tag-rds'}">
          ${db.is_aurora ? '⚡ Aurora Cluster' : '📦 RDS Instance'}
        </span>
      </div>

      <div class="card-meta-grid">
        <div class="meta-item">
          <span class="meta-label">Engine</span>
          <span class="meta-value">${escapeHtml(db.engine)}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Current Version</span>
          <span class="meta-value" style="font-weight: 700; color: ${isCompleted ? 'var(--accent-green)' : 'var(--text-primary)'};">${escapeHtml(db.version)} ${isCompleted ? '✅' : ''}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Status</span>
          <span class="status-badge ${isUpgrading ? 'upgrading' : (isCompleted || db.aws_status === 'available' ? 'available' : 'stopped')}">
            ${isUpgrading ? '<span class="pulse-dot"></span> Upgrading' : (isCompleted ? '● Upgraded (Live)' : (isOldInstance ? '● Decommissioned' : `● ${escapeHtml(db.aws_status || db.status)}`))}
          </span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Parameter Group</span>
          <span class="meta-value" style="font-size: 0.8rem;" title="${escapeHtml(db.parameter_group || '')}">${escapeHtml(db.parameter_group || 'default')}</span>
        </div>
      </div>

      <div class="card-actions" style="display: flex; flex-wrap: wrap; gap: 8px;">
        ${isOldInstance ? `
          <button class="btn btn-secondary" style="color: #f59e0b; border-color: rgba(245, 158, 11, 0.4);" onclick="stopDatabase('${escapeHtml(db.id)}', ${db.is_cluster})">
            ⏸️ Stop Old DB
          </button>
          <button class="btn btn-secondary" style="color: #ef4444; border-color: rgba(239, 68, 68, 0.4);" onclick="deleteDatabase('${escapeHtml(db.id)}', ${db.is_cluster})">
            🗑️ Delete Old DB
          </button>
        ` : `
          <button class="btn btn-secondary" onclick="openAuditModal('${escapeHtml(db.id)}')">
            🛡️ AI Audit
          </button>
          ${isUpgrading ? `
            <button class="btn btn-upgrading" onclick="selectDeployment('${escapeHtml(db.id)}'); switchTab('pipelinePane');" title="Click to view live upgrade progress">
              <span class="pulse-dot"></span> ⏳ Upgrading...
            </button>
          ` : `
            <button class="btn btn-primary" onclick="openAuditModal('${escapeHtml(db.id)}')">
              ${isCompleted ? '🚀 Upgrade Further' : '🚀 Upgrade'}
            </button>
            ${isCompleted ? `
              <button class="btn btn-secondary" onclick="selectDeployment('${escapeHtml(db.id)}'); switchTab('pipelinePane');" title="View Completed Switchover details">
                ⚙️ Mission Control
              </button>
            ` : ''}
          `}
        `}
      </div>
    </div>
  `;
  }).join('');
}

// Pre-Flight Audit Modal
async function openAuditModal(dbId, selectedTarget = null) {
  selectedAuditDb = dbId;
  const modalBody = document.getElementById('auditModalBody');
  document.getElementById('auditModalTitle').textContent = `🛡️ Pre-Flight Audit: ${dbId}`;
  
  if (!selectedTarget) {
    modalBody.innerHTML = '<div style="padding: 1.5rem; color: var(--text-muted);">Running AI pre-flight parameter & topology audit...</div>';
    openModal('auditModal');
  }

  try {
    const url = selectedTarget 
      ? `/api/databases/${dbId}/audit?target_version=${encodeURIComponent(selectedTarget)}` 
      : `/api/databases/${dbId}/audit`;
    const res = await fetch(url);
    const data = await res.json();
    const audit = data.audit;

    const availableTargets = audit.available_targets || [];
    const majorTargets = availableTargets.filter(t => t.IsMajorVersionUpgrade);
    const minorTargets = availableTargets.filter(t => !t.IsMajorVersionUpgrade);

    modalBody.innerHTML = `
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.25rem;">
        <div style="display: flex; gap: 0.5rem; align-items: center;">
          <span style="font-size: 0.85rem; font-weight: 600; color: var(--text-muted);">Risk Score:</span>
          <span class="status-badge ${audit.risk_score === 'LOW' ? 'available' : (audit.risk_score === 'BLOCKING' ? 'stopped' : 'upgrading')}" style="font-size: 0.85rem; padding: 4px 10px;">
            ${audit.risk_score}
          </span>
        </div>
        <div style="display: flex; align-items: center; gap: 0.5rem;">
          <label style="font-size: 0.85rem; font-weight: 600; color: var(--accent-cyan);">🎯 Target Version:</label>
          <select id="modalTargetVersionSelect" class="chat-input" style="padding: 4px 8px; font-size: 0.85rem; font-weight: 600; color: var(--accent-cyan); border-color: var(--accent-cyan); width: auto;">
            ${availableTargets.length > 0 ? `
              ${majorTargets.length > 0 ? `
                <optgroup label="🚀 Major Version Upgrades">
                  ${majorTargets.map(t => `<option value="${t.EngineVersion}" ${t.EngineVersion === audit.target_version ? 'selected' : ''}>PostgreSQL ${t.EngineVersion} (Major Upgrade)</option>`).join('')}
                </optgroup>
              ` : ''}
              ${minorTargets.length > 0 ? `
                <optgroup label="🔧 Minor Version Upgrades">
                  ${minorTargets.map(t => `<option value="${t.EngineVersion}" ${t.EngineVersion === audit.target_version ? 'selected' : ''}>PostgreSQL ${t.EngineVersion} (Minor Upgrade)</option>`).join('')}
                </optgroup>
              ` : ''}
            ` : `<option value="${audit.target_version}" selected>${audit.target_version}</option>`}
          </select>
        </div>
      </div>

      <div class="card-meta-grid" style="margin-top: 0;">
        <div class="meta-item">
          <span class="meta-label">Engine & Current Version</span>
          <span class="meta-value">${audit.engine} ${audit.current_version}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Selected Target Version</span>
          <span class="meta-value" style="color: var(--accent-cyan);">${audit.target_version} (${audit.target_family})</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Source Parameter Group</span>
          <span class="meta-value">${audit.source_parameter_group}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Auto-Scaling Policies</span>
          <span class="meta-value">${audit.scaling_policies_count} Detected</span>
        </div>
      </div>

      ${audit.risk_factors.length > 0 ? `
        <div style="margin-top: 1rem;">
          <div style="font-size: 0.85rem; font-weight: 700; color: var(--accent-amber); margin-bottom: 0.5rem;">⚠️ Findings & Auto-Remediations:</div>
          ${audit.risk_factors.map(rf => `
            <div style="background: rgba(255,255,255,0.03); border: 1px solid var(--border-subtle); padding: 0.75rem; border-radius: 8px; margin-bottom: 0.5rem; font-size: 0.85rem;">
              <strong>${rf.title}:</strong> ${rf.description}
            </div>
          `).join('')}
        </div>
      ` : ''}

      <div style="margin-top: 1rem;">
        <div style="font-size: 0.85rem; font-weight: 700; color: var(--accent-cyan); margin-bottom: 0.5rem;">⚙️ Automated Parameter Enforcements:</div>
        <table style="width: 100%; font-size: 0.8rem; border-collapse: collapse; background: var(--bg-surface-elevated); border-radius: 8px;">
          <thead>
            <tr style="border-bottom: 1px solid var(--border-subtle); text-align: left;">
              <th style="padding: 6px 10px;">Parameter</th>
              <th style="padding: 6px 10px;">Enforced Value</th>
              <th style="padding: 6px 10px;">Reason</th>
            </tr>
          </thead>
          <tbody>
            ${audit.static_enforcements.map(se => `
              <tr style="border-bottom: 1px solid var(--border-subtle);">
                <td style="padding: 6px 10px; font-family: var(--font-mono); color: var(--accent-cyan);">${se.parameter}</td>
                <td style="padding: 6px 10px; font-weight: 600;">${se.target_enforced_value}</td>
                <td style="padding: 6px 10px; color: var(--text-muted);">${se.reason}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    `;

    // Hook up dynamic version switcher in the modal
    const versionSelect = document.getElementById('modalTargetVersionSelect');
    if (versionSelect) {
      versionSelect.addEventListener('change', (e) => {
        openAuditModal(dbId, e.target.value);
      });
    }

    document.getElementById('launchUpgradeBtn').onclick = () => {
      const selectedVer = document.getElementById('modalTargetVersionSelect') 
        ? document.getElementById('modalTargetVersionSelect').value 
        : audit.target_version;
      closeModal('auditModal');
      quickLaunchUpgrade(dbId, selectedVer);
    };

  } catch (e) {
    modalBody.innerHTML = `<div style="color: var(--accent-rose);">Audit error: ${e.message}</div>`;
  }
}

// Upgrade Pipeline Launcher
async function quickLaunchUpgrade(dbId, targetVersion = null) {
  switchTab('pipelinePane');
  
  document.getElementById('activeTaskBadge').textContent = `Starting upgrade for ${dbId}...`;
  document.getElementById('pipelineTargetDb').textContent = dbId;
  document.getElementById('pipelineTargetVersion').textContent = targetVersion || 'Auto (Next Major)';
  document.getElementById('pipelinePhase').textContent = 'Initializing';
  document.getElementById('cutoverGatePanel').style.display = 'none';

  const terminal = document.getElementById('terminalLogs');
  terminal.innerHTML = `
    <div class="log-line">
      <span class="log-time">[${new Date().toLocaleTimeString()}]</span>
      <span class="log-msg-header">🚀 Launching Blue/Green Upgrade Engine for ${dbId}...</span>
    </div>
  `;

  resetStepper();

  try {
    const res = await fetch('/api/upgrade/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        db_identifier: dbId,
        target_version: targetVersion,
        auto_reboot: true,
        auto_deploy: true,
        auto_switchover: false,
        auto_stop_old_db: false
      })
    });
    const data = await res.json();
    activeTaskId = data.task_id;
    fetchUpgradeTasks();
    startTaskPolling();
  } catch (e) {
    terminal.innerHTML += `
      <div class="log-line">
        <span class="log-time">[${new Date().toLocaleTimeString()}]</span>
        <span class="log-msg-error">Failed to launch upgrade: ${e.message}</span>
      </div>
    `;
  }
}

let currentTaskStages = null;
let inspectedStageNum = '1';
let selectedDbIdentifier = null; // Dynamically set to active task or user selection
let masterFilterCategory = 'all';
let masterSearchQuery = '';

async function fetchUpgradeTasks() {
  try {
    const res = await fetch('/api/upgrade/tasks');
    const data = await res.json();
    const tasks = data.tasks || [];
    allTasks = tasks;
    renderMasterDeployments(tasks);
    if (currentFleet.length > 0) {
      renderFleet(currentFleet);
    }
    
    // If no active DB is selected yet, lock onto the active task or first deployment
    if (!selectedDbIdentifier && tasks.length > 0) {
      const activeRunning = tasks.find(t => t.status !== 'COMPLETED' && t.status !== 'FAILED');
      selectDeployment(activeRunning ? activeRunning.db_identifier : tasks[0].db_identifier);
    }
  } catch (e) {
    console.error('Error fetching tasks:', e);
  }
}

function renderMasterDeployments(tasks) {
  const container = document.getElementById('masterDeploymentsList');
  const countBadge = document.getElementById('pipelineCountBadge');
  if (countBadge) countBadge.textContent = tasks.length;
  if (!container) return;

  if (tasks.length === 0) {
    container.innerHTML = '<div style="color: var(--text-muted); font-size: 0.85rem; padding: 1rem; text-align: center;">No active upgrades running.</div>';
    return;
  }

  // Ensure initial pinned DB is set
  if (!selectedDbIdentifier && tasks.length > 0) {
    selectedDbIdentifier = tasks[0].db_identifier;
  }

  // Filter tasks based on category & search query
  let filtered = tasks.filter(t => {
    if (masterFilterCategory === 'ready' && t.status !== 'WAITING_SWITCHOVER_APPROVAL') return false;
    if (masterFilterCategory === 'provisioning' && t.status !== 'PROVISIONING_GREEN' && t.status !== 'PROVISIONING') return false;
    if (masterFilterCategory === 'rds' && t.is_cluster) return false;
    if (masterFilterCategory === 'aurora' && !t.is_cluster) return false;

    if (masterSearchQuery) {
      const matchDb = (t.db_identifier || '').toLowerCase().includes(masterSearchQuery);
      const matchVer = String(t.target_version || '').toLowerCase().includes(masterSearchQuery);
      const matchStatus = String(t.status || '').toLowerCase().includes(masterSearchQuery);
      const matchBg = String(t.bg_id || '').toLowerCase().includes(masterSearchQuery);
      if (!matchDb && !matchVer && !matchStatus && !matchBg) return false;
    }
    return true;
  });

  if (filtered.length === 0) {
    container.innerHTML = '<div style="color: var(--text-muted); font-size: 0.85rem; padding: 1rem; text-align: center;">No matching deployments found.</div>';
    return;
  }

  container.innerHTML = filtered.map(t => {
    const isSelected = (t.db_identifier === selectedDbIdentifier || t.task_id === selectedDbIdentifier || t.bg_id === selectedDbIdentifier);
    const isCluster = t.is_cluster;
    const isReady = t.status === 'WAITING_SWITCHOVER_APPROVAL';
    const isCompleted = t.status === 'COMPLETED';
    const isFailed = t.status === 'FAILED';
    
    let statusClass = 'upgrading';
    let statusLabel = '⚡ Syncing';
    if (isReady) { statusClass = 'available'; statusLabel = '✅ Cutover Ready'; }
    else if (isCompleted) { statusClass = 'available'; statusLabel = '🎉 Upgraded'; }
    else if (isFailed) { statusClass = 'stopped'; statusLabel = '❌ Failed'; }

    return `
      <div class="master-deploy-card ${isSelected ? 'active' : ''}" data-db-id="${escapeHtml(t.db_identifier)}" onclick="selectDeployment('${escapeHtml(t.db_identifier)}')">
        <div class="master-card-top">
          <span class="master-card-name">${escapeHtml(t.db_identifier)}</span>
          <span class="card-type-tag ${isCluster ? 'tag-aurora' : 'tag-rds'}" style="font-size: 0.7rem; padding: 2px 6px;">
            ${isCluster ? '⚡ Aurora' : '📦 RDS'}
          </span>
        </div>

        <div class="master-card-migration">
          <span>v${escapeHtml(t.current_version || t.source_version || 'Live')}</span>
          <span style="color: var(--accent-cyan);">➔</span>
          <span style="color: var(--accent-cyan); font-weight: 700;">${escapeHtml(t.target_version ? `v${t.target_version}` : 'Upgrade')}</span>
        </div>

        <div class="master-card-progress">
          <div class="master-card-progress-bar" style="width: ${t.progress || 55}%;"></div>
        </div>

        <div class="master-card-footer">
          <span class="status-badge ${statusClass}" style="font-size: 0.72rem;">
            ${(isReady || isCompleted) ? '●' : '<span class="pulse-dot"></span>'} ${statusLabel}
          </span>
          <span style="font-family: var(--font-mono); font-size: 0.72rem; color: var(--text-faint);">
            ${escapeHtml(t.bg_id ? t.bg_id.substring(0, 14) + '...' : (t.task_id || '').substring(0, 10))}
          </span>
        </div>
      </div>
    `;
  }).join('');
}

function selectDeployment(dbId) {
  selectedDbIdentifier = dbId;
  activeTaskId = dbId;
  
  // Highlight the active card in the master list
  document.querySelectorAll('.master-deploy-card').forEach(p => {
    if (p.dataset.dbId === dbId) {
      p.classList.add('active');
    } else {
      p.classList.remove('active');
    }
  });

  pollActiveTaskStatus();
}

function selectActiveTask(taskId) {
  selectDeployment(taskId);
}

function startTaskPolling() {
  if (pollInterval) clearInterval(pollInterval);
  pollInterval = setInterval(() => {
    fetchUpgradeTasks();
    pollActiveTaskStatus();
  }, 4000);
}

async function pollActiveTaskStatus() {
  const targetId = selectedDbIdentifier || activeTaskId;
  if (!targetId) return;

  try {
    const res = await fetch(`/api/upgrade/status/${encodeURIComponent(targetId)}`);
    if (!res.ok) return;
    const data = await res.json();

    const isCluster = data.is_cluster;
    const isReady = data.status === 'WAITING_SWITCHOVER_APPROVAL';
    const isCompleted = data.status === 'COMPLETED';
    const isDeleting = data.status === 'DELETING' || (data.phase && data.phase.toLowerCase().includes('delet'));

    // 1. Hero Header
    document.getElementById('heroDbTitle').textContent = data.db_identifier;
    
    const heroTypeTag = document.getElementById('heroTypeTag');
    heroTypeTag.className = `card-type-tag ${isCluster ? 'tag-aurora' : 'tag-rds'}`;
    heroTypeTag.textContent = isCluster ? '⚡ Aurora Cluster' : '📦 Standalone RDS';

    const heroStatusBadge = document.getElementById('heroStatusBadge');
    if (isReady) {
      heroStatusBadge.className = 'status-badge available';
      heroStatusBadge.innerHTML = '● CUTOVER READY';
    } else if (isCompleted) {
      heroStatusBadge.className = 'status-badge available';
      heroStatusBadge.innerHTML = '🎉 COMPLETED';
    } else if (isDeleting) {
      heroStatusBadge.className = 'status-badge stopped';
      heroStatusBadge.innerHTML = '🗑️ DELETING B/G';
    } else {
      heroStatusBadge.className = 'status-badge upgrading';
      heroStatusBadge.innerHTML = `<span class="pulse-dot"></span> ${escapeHtml(data.phase || 'PROVISIONING REPLICA')}`;
    }

    const stage1Details = (data.stages && data.stages['1'] && data.stages['1'].details) || [];
    const engineDetail = stage1Details.find(d => d.label.includes('Engine'));
    const srcVerText = engineDetail ? engineDetail.value.replace(/postgres(?:ql)?\s*v?/i, '').trim() : (data.current_version || data.source_version || (isCluster ? '15.13' : '18.1'));

    document.getElementById('heroDeploymentId').textContent = data.bg_id || data.task_id;
    document.getElementById('heroEngineMigration').textContent = `${isCluster ? 'Aurora PostgreSQL' : 'PostgreSQL'} ${srcVerText} ➔ ${data.target_version || 'Target Upgrade'}`;

    // Hero Action Area
    const actionArea = document.getElementById('heroCutoverActionArea');
    if (actionArea) {
      if (isReady) {
        actionArea.innerHTML = `
          <button class="btn btn-primary" style="padding: 0.75rem 1.5rem; font-size: 0.95rem; box-shadow: 0 0 20px rgba(0, 242, 254, 0.4);" onclick="executeSwitchover('${encodeURIComponent(data.db_identifier)}')">
            🚀 Approve & Execute DNS Switchover
          </button>
        `;
      } else if (isCompleted || isDeleting) {
        const oldDb = data.old_db_identifier || `${data.db_identifier}-old1`;
        actionArea.innerHTML = `
          <div style="display: flex; flex-wrap: wrap; gap: 8px; align-items: center;">
            <button class="btn btn-primary" onclick="openAuditModal('${encodeURIComponent(data.db_identifier)}')">
              🚀 Upgrade Further
            </button>
            ${(data.bg_id && !isDeleting) ? `
              <button class="btn btn-secondary" style="border-color: rgba(0, 242, 254, 0.4);" onclick="cleanupBg('${encodeURIComponent(data.bg_id)}')">
                🧹 Delete B/G Deployment
              </button>
            ` : ''}
            <button class="btn btn-secondary" style="color: #f59e0b; border-color: rgba(245, 158, 11, 0.4);" onclick="stopDatabase('${encodeURIComponent(oldDb)}', ${isCluster})">
              ⏸️ Stop Old DB (${escapeHtml(oldDb)})
            </button>
            <button class="btn btn-secondary" style="color: #ef4444; border-color: rgba(239, 68, 68, 0.4);" onclick="deleteDatabase('${encodeURIComponent(oldDb)}', ${isCluster})">
              🗑️ Delete Old DB (${escapeHtml(oldDb)})
            </button>
          </div>
        `;
      } else {
        actionArea.innerHTML = `
          <div class="status-badge upgrading" style="font-size: 0.85rem; padding: 6px 14px;">
            <span class="pulse-dot"></span> ${escapeHtml(data.phase || 'Provisioning Green Replica')}
          </div>
        `;
      }
    }

    // 2. Topology Diagram
    const bluePg = data.source_pg || (stage1Details.find(d => d.label.includes('Parameter Group')) ? stage1Details.find(d => d.label.includes('Parameter Group')).value : `${data.db_identifier}-pg`);
    document.getElementById('topoBlueId').textContent = data.db_identifier;
    document.getElementById('topoBlueVer').textContent = `${isCluster ? 'aurora-postgresql' : 'postgres'} v${srcVerText}`;
    document.getElementById('topoBluePg').textContent = bluePg;

    const stage3Details = (data.stages && data.stages['3'] && data.stages['3'].details) || [];
    const greenItem = stage3Details.find(d => d.label.includes('Target Replica'));
    const greenTargetName = greenItem ? greenItem.value : (data.target_id || `${data.db_identifier}-green-target`);

    document.getElementById('topoGreenId').textContent = greenTargetName.split(' ')[0];
    document.getElementById('topoGreenVer').textContent = `Target: PostgreSQL ${data.target_version || 'Target'}`;
    document.getElementById('topoGreenPg').textContent = `${data.db_identifier}-pg-target`;

    // 3. Stage Diagnostics & Stepper
    currentTaskStages = data.stages;
    renderStageInspectionDetails();
    updateStepper(data.progress);
    renderLogs(data.logs);

  } catch (e) {
    console.error('Error polling status:', e);
  }
}

async function executeSwitchover(taskId) {
  if (!confirm('Are you sure you want to approve DNS cutover to the Green database?')) return;

  const actionArea = document.getElementById('heroCutoverActionArea');
  if (actionArea) {
    actionArea.innerHTML = '<button class="btn btn-primary" disabled style="background: linear-gradient(135deg, #f59e0b, #d97706);">⏳ Initiating DNS Cutover Swap...</button>';
  }

  try {
    const res = await fetch('/api/upgrade/switchover', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId })
    });
    const data = await res.json();
    if (data.status === 'error') {
      alert('Cutover error: ' + data.message);
    } else {
      setTimeout(() => {
        fetchUpgradeTasks();
        pollActiveTaskStatus();
      }, 500);
    }
  } catch (e) {
    alert('Switchover request failed: ' + e.message);
  }
}

async function cleanupBg(bgId) {
  if (!confirm(`Are you sure you want to delete Blue/Green Deployment '${bgId}' on AWS?\n\n(Note: The new upgraded live Production database is preserved and untouched).`)) return;
  try {
    const res = await fetch('/api/bg/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bg_id: bgId, delete_target: false })
    });
    const data = await res.json();
    alert(data.message || 'B/G deployment deletion submitted');
    fetchFleet();
    fetchUpgradeTasks();
    pollActiveTaskStatus();
  } catch (e) {
    alert('Failed to delete B/G deployment: ' + e.message);
  }
}

async function stopDatabase(dbId, isCluster = false) {
  if (!confirm(`Are you sure you want to shut down decommissioned database '${dbId}' to save AWS hourly costs?`)) return;
  try {
    const res = await fetch('/api/db/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ db_identifier: dbId, is_cluster: isCluster })
    });
    const data = await res.json();
    alert(data.message || 'Shutdown command sent');
    fetchFleet();
    pollActiveTaskStatus();
  } catch (e) {
    alert('Failed to stop database: ' + e.message);
  }
}

async function deleteDatabase(dbId, isCluster = false) {
  if (!confirm(`⚠️ DANGER: Are you sure you want to permanently DELETE old database '${dbId}'?\n\nThis action cannot be undone.`)) return;
  try {
    const res = await fetch('/api/db/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ db_identifier: dbId, is_cluster: isCluster, skip_final_snapshot: true })
    });
    const data = await res.json();
    alert(data.message || 'Deletion command sent');
    fetchFleet();
    pollActiveTaskStatus();
  } catch (e) {
    alert('Failed to delete database: ' + e.message);
  }
}

function fetchDbStatus(dbId) {
  if (dbId) selectedDbIdentifier = dbId;
  return pollActiveTaskStatus();
}

function inspectStage(stageNum) {
  inspectedStageNum = String(stageNum);
  
  // Highlight the inspected step in the stepper
  document.querySelectorAll('.step-item').forEach(el => el.classList.remove('inspecting'));
  const targetStep = document.getElementById(`step${stageNum}`);
  if (targetStep) targetStep.classList.add('inspecting');

  // Highlight the active stage-nav-pill
  document.querySelectorAll('.stage-nav-pill').forEach((pill, idx) => {
    if (String(idx + 1) === String(stageNum)) {
      pill.classList.add('active');
    } else {
      pill.classList.remove('active');
    }
  });

  renderStageInspectionDetails();
}

function renderStageInspectionDetails() {
  const titleEl = document.getElementById('stageInspectorTitle');
  const badgeEl = document.getElementById('stageInspectorBadge');
  const summaryEl = document.getElementById('stageInspectorSummary');
  const detailsEl = document.getElementById('stageInspectorDetails');

  if (!titleEl || !detailsEl) return;

  if (!currentTaskStages || !currentTaskStages[inspectedStageNum]) {
    titleEl.textContent = `🔍 Stage ${inspectedStageNum}: Diagnostics & Validation`;
    badgeEl.textContent = 'VALIDATING';
    badgeEl.className = 'status-badge available';
    summaryEl.textContent = 'Auditing and validating database parameters and AWS Blue/Green state...';
    detailsEl.innerHTML = '<div style="color: var(--text-muted); font-size: 0.85rem;">Stage diagnostics loading...</div>';
    return;
  }

  const s = currentTaskStages[inspectedStageNum];
  titleEl.innerHTML = `🔍 Stage ${inspectedStageNum}: ${escapeHtml(s.title || s.name)}`;
  
  let badgeClass = 'available';
  let badgeText = s.status || 'COMPLETED';
  if (s.status === 'IN_PROGRESS') { badgeClass = 'upgrading'; badgeText = '⚡ IN PROGRESS'; }
  else if (s.status === 'WAITING_APPROVAL') { badgeClass = 'upgrading'; badgeText = '⚠️ CUTOVER GATE READY'; }
  else if (s.status === 'PENDING') { badgeClass = 'stopped'; badgeText = '⏳ PENDING'; }
  else { badgeClass = 'available'; badgeText = '✅ VALIDATED & PASSED'; }

  badgeEl.className = `status-badge ${badgeClass}`;
  badgeEl.textContent = badgeText;

  summaryEl.textContent = s.summary || '';

  let html = '';

  // Checklist / Parameters table
  if (s.details && s.details.length > 0) {
    html += `
      <table class="validation-table">
        <thead>
          <tr>
            <th style="width: 38%;">Validation Parameter / Check</th>
            <th style="width: 47%;">Verified Value & Telemetry</th>
            <th style="width: 15%;">Status</th>
          </tr>
        </thead>
        <tbody>
          ${s.details.map(d => {
            const statusClass = (d.status === 'PASS') ? 'pass' : ((d.status === 'INFO') ? 'info' : ((d.status === 'IN_PROGRESS') ? 'in_progress' : 'waiting'));
            const icon = (d.status === 'PASS') ? '✓' : ((d.status === 'INFO') ? 'ℹ' : '⏳');
            return `
              <tr>
                <td style="font-weight: 600; color: var(--text-main); font-family: var(--font-mono);">${escapeHtml(d.label)}</td>
                <td style="color: var(--accent-cyan); font-family: var(--font-mono);">${escapeHtml(d.value)}</td>
                <td><span class="val-badge ${statusClass}">${icon} ${d.status}</span></td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    `;
  }

  // AWS Subtasks list (if present in stage)
  if (s.subtasks && s.subtasks.length > 0) {
    html += `
      <div style="margin-top: 1rem;">
        <div style="font-size: 0.82rem; font-weight: 700; color: var(--text-muted); text-transform: uppercase; margin-bottom: 0.4rem;">
          AWS Blue/Green Orchestration Subtasks:
        </div>
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 0.5rem;">
          ${s.subtasks.map(st => {
            const isDone = st.status === 'COMPLETED';
            const isRun = st.status === 'IN_PROGRESS';
            return `
              <div style="background: var(--bg-surface-elevated); border: 1px solid var(--border-subtle); padding: 0.6rem 0.8rem; border-radius: 8px; display: flex; justify-content: space-between; align-items: center;">
                <span style="font-family: var(--font-mono); font-size: 0.78rem; color: var(--text-main);">${escapeHtml(st.name)}</span>
                <span class="status-badge ${isDone ? 'available' : (isRun ? 'upgrading' : 'stopped')}" style="font-size: 0.72rem;">
                  ${isDone ? '✅ COMPLETED' : (isRun ? '<span class="pulse-dot"></span> IN_PROGRESS' : '⏳ PENDING')}
                </span>
              </div>
            `;
          }).join('')}
        </div>
      </div>
    `;
  }

  detailsEl.innerHTML = html;
}

function updateStepper(progress) {
  const steps = [
    { id: 'step1', min: 10 },
    { id: 'step2', min: 30 },
    { id: 'step3', min: 50 },
    { id: 'step4', min: 75 },
    { id: 'step5', min: 90 },
  ];

  steps.forEach(s => {
    const el = document.getElementById(s.id);
    if (!el) return;
    const isInspecting = (s.id === `step${inspectedStageNum}`);
    let cls = 'step-item';
    if (progress >= s.min) {
      cls += (progress > s.min + 15) ? ' completed' : ' active';
    }
    if (isInspecting) {
      cls += ' inspecting';
    }
    el.className = cls;
  });
}

function resetStepper() {
  document.querySelectorAll('.step-item').forEach(el => el.className = 'step-item');
}

function renderLogs(logs) {
  const terminal = document.getElementById('terminalLogs');
  terminal.innerHTML = logs.map(l => {
    let msgClass = 'log-msg-info';
    if (l.level === 'HEADER') msgClass = 'log-msg-header';
    if (l.level === 'SUCCESS') msgClass = 'log-msg-success';
    if (l.level === 'ERROR') msgClass = 'log-msg-error';
    if (l.level === 'GATE') msgClass = 'log-msg-gate';

    return `
      <div class="log-line">
        <span class="log-time">[${l.time}]</span>
        <span class="${msgClass}">${escapeHtml(l.message)}</span>
      </div>
    `;
  }).join('');
  terminal.scrollTop = terminal.scrollHeight;
}

// Cutover Confirmation
document.getElementById('approveSwitchoverBtn').addEventListener('click', async () => {
  if (!activeTaskId) return;
  document.getElementById('approveSwitchoverBtn').disabled = true;
  document.getElementById('approveSwitchoverBtn').textContent = '⏳ Executing DNS Switchover...';

  try {
    await fetch('/api/upgrade/switchover', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: activeTaskId })
    });
  } catch (e) {
    alert('Switchover error: ' + e.message);
  }
});

// AI Copilot Persistent Chat & Multi-Session Management
function initChat() {
  const input = document.getElementById('chatInput');
  const sendBtn = document.getElementById('sendChatBtn');
  const dbSelect = document.getElementById('chatDbSelect');

  // Load chat sessions & active chat from backend on startup
  loadChatSessions();

  if (sendBtn) sendBtn.addEventListener('click', () => sendChatMessage());
  if (input) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') sendChatMessage();
    });
  }

  if (dbSelect) {
    dbSelect.addEventListener('change', (e) => {
      const dbId = e.target.value;
      if (dbId) {
        openDbChat(dbId);
        e.target.value = '';
      }
    });
  }

  document.querySelectorAll('.prompt-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      input.value = chip.dataset.prompt;
      sendChatMessage();
    });
  });

  // Profile modal save
  const saveProfBtn = document.getElementById('saveProfileBtn');
  if (saveProfBtn) {
    saveProfBtn.addEventListener('click', saveProfile);
  }

  // Rename modal confirm
  const confirmRenBtn = document.getElementById('confirmRenameBtn');
  if (confirmRenBtn) {
    confirmRenBtn.addEventListener('click', executeRenameDb);
  }
}

async function loadChatSessions() {
  try {
    const res = await fetch('/api/chat/sessions');
    if (!res.ok) return;
    const data = await res.json();
    
    currentChatSessions = data.sessions || [];
    activeSessionId = data.active_session_id || (currentChatSessions[0] ? currentChatSessions[0].id : 'session_default');
    
    renderSessionsList();
    loadActiveSessionMessages(activeSessionId);
  } catch (e) {
    console.error('Failed to load chat sessions:', e);
  }
}

function updateChatDbDropdown(databases) {
  const select = document.getElementById('chatDbSelect');
  if (!select) return;
  
  const currentVal = select.value;
  select.innerHTML = '<option value="">🎯 New Thread for DB...</option>' + 
    (databases || []).map(db => `<option value="${escapeHtml(db.id)}">🎯 ${escapeHtml(db.id)} (${db.engine || 'RDS'})</option>`).join('');
  select.value = currentVal;
}

function renderSessionsList() {
  const container = document.getElementById('sessionsListContainer');
  if (!container) return;

  if (!currentChatSessions || currentChatSessions.length === 0) {
    container.innerHTML = '<div style="padding: 0.75rem; color: var(--text-faint); font-size: 0.8rem;">No active threads. Click + New Chat to begin.</div>';
    return;
  }

  container.innerHTML = currentChatSessions.map(s => {
    const isActive = s.id === activeSessionId;
    const isDefault = s.id === 'session_default';
    return `
      <div class="session-item ${isActive ? 'active' : ''}" onclick="switchChatSession('${escapeHtml(s.id)}')">
        <div class="session-item-content">
          <div class="session-item-title" title="${escapeHtml(s.title)}">
            ${s.db_identifier ? '📦' : '💬'} ${escapeHtml(s.title)}
          </div>
          <div class="session-item-sub">
            <span class="session-msg-badge">${s.message_count || 0} msgs</span>
            ${s.db_identifier ? `<span class="session-db-badge" title="${escapeHtml(s.db_identifier)}">${escapeHtml(s.db_identifier)}</span>` : ''}
          </div>
        </div>
        ${!isDefault ? `
          <button class="session-delete-btn" title="Delete thread" onclick="deleteChatSession('${escapeHtml(s.id)}', event)">✕</button>
        ` : ''}
      </div>
    `;
  }).join('');
}

async function switchChatSession(sessionId) {
  activeSessionId = sessionId;
  renderSessionsList();
  await loadActiveSessionMessages(sessionId);
}

async function loadActiveSessionMessages(sessionId) {
  try {
    const res = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}`);
    if (!res.ok) return;
    const data = await res.json();
    
    const sess = data.session || data;

    if (data.user_profile) {
      currentUserProfile = data.user_profile;
      updateUserProfileHeader();
    }
    
    // Update active thread title in chat header
    const titleEl = document.getElementById('activeSessionTitle');
    const badgeEl = document.getElementById('activeSessionDbBadge');
    if (titleEl) titleEl.textContent = sess.title || data.title || 'General Fleet Copilot';
    if (badgeEl) {
      const dbId = sess.db_identifier || data.db_identifier;
      if (dbId) {
        badgeEl.textContent = `🎯 ${dbId}`;
        badgeEl.style.display = 'inline-block';
      } else {
        badgeEl.style.display = 'none';
      }
    }

    const msgs = sess.messages || data.messages || [];
    renderAllChatMessages(msgs);
  } catch (e) {
    console.error('Failed to load session messages:', e);
  }
}

async function createNewChatSession(customTitle = null, dbIdentifier = null) {
  try {
    const sessionCount = (Array.isArray(currentChatSessions) ? currentChatSessions.length : 0) + 1;
    const title = customTitle || (dbIdentifier ? `${dbIdentifier} Upgrade & Ops` : `Chat Session #${sessionCount}`);
    const res = await fetch('/api/chat/sessions/new', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: title,
        db_identifier: dbIdentifier || null
      })
    });
    const data = await res.json();
    if (data.session) {
      activeSessionId = data.session.id;
      await loadChatSessions();
    }
  } catch (e) {
    alert('Failed to create new chat thread: ' + e.message);
  }
}

async function deleteChatSession(sessionId, event) {
  if (event) event.stopPropagation();
  if (!confirm('Are you sure you want to delete this chat thread?')) return;
  
  try {
    const res = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}`, {
      method: 'DELETE'
    });
    const data = await res.json();
    currentChatSessions = data.sessions || [];
    activeSessionId = data.active_session_id || 'session_default';
    renderSessionsList();
    await loadActiveSessionMessages(activeSessionId);
  } catch (e) {
    alert('Failed to delete chat thread: ' + e.message);
  }
}

async function openDbChat(dbId) {
  switchTab('chatPane');
  
  // Find existing session for this DB
  const existing = currentChatSessions.find(s => s.db_identifier === dbId);
  if (existing) {
    await switchChatSession(existing.id);
  } else {
    await createNewChatSession(`${dbId} Upgrade & Ops`, dbId);
  }
}

function updateUserProfileHeader() {
  const avEl = document.getElementById('chatUserAvatar');
  const nameEl = document.getElementById('chatUserName');
  const roleEl = document.getElementById('chatUserRoleBadge');
  
  if (avEl) avEl.textContent = currentUserProfile.avatar || '👨‍💻';
  if (nameEl) nameEl.textContent = currentUserProfile.name || 'Cloud DevOps Lead';
  if (roleEl) roleEl.textContent = currentUserProfile.role || 'Database Administrator';
}

function renderAllChatMessages(messages) {
  const container = document.getElementById('chatMessages');
  if (!container) return;
  
  const validMessages = (messages || []).filter(msg => {
    if (!msg) return false;
    const hasText = msg.text && msg.text.trim().length > 0;
    const hasActions = msg.action_buttons && msg.action_buttons.length > 0;
    return hasText || hasActions;
  });

  container.innerHTML = validMessages.map(msg => {
    const isUser = msg.sender === 'user';
    const avatar = msg.avatar || (isUser ? currentUserProfile.avatar : '🤖');
    const name = msg.user_name || (isUser ? currentUserProfile.name : 'RDS AI Copilot');
    const role = msg.user_role || (isUser ? currentUserProfile.role : 'AI Agent');
    const timeStr = msg.timestamp || '';
    
    const actionsHtml = (msg.action_buttons && msg.action_buttons.length > 0) ? `
      <div class="chat-action-buttons">
        ${msg.action_buttons.map(btn => {
          let btnClass = 'chat-action-btn btn-primary';
          if (btn.style === 'danger') btnClass = 'chat-action-btn btn-danger';
          if (btn.style === 'warning') btnClass = 'chat-action-btn btn-warning';
          if (btn.style === 'secondary') btnClass = 'chat-action-btn btn-secondary';
          
          const paramsStr = btn.params ? escapeHtml(JSON.stringify(btn.params)) : '';
          return `
            <button class="${btnClass}" data-action="${escapeHtml(btn.action)}" data-target="${escapeHtml(btn.target || '')}" data-version="${escapeHtml(btn.target_version || '')}" data-cluster="${btn.is_cluster || false}" data-params="${paramsStr}" onclick="onChatActionButtonClick(this)">
              ${escapeHtml(btn.label)}
            </button>
          `;
        }).join('')}
      </div>
    ` : '';

    return `
      <div id="${msg.id}" class="message-bubble message-${msg.sender}">
        <div class="chat-bubble-header">
          <div class="chat-bubble-user-info">
            <span>${escapeHtml(avatar)}</span>
            <span>${escapeHtml(name)}</span>
            <span class="val-badge ${isUser ? 'info' : 'pass'}" style="font-size: 0.65rem; padding: 1px 5px;">${escapeHtml(role)}</span>
          </div>
          <span class="chat-bubble-time">${escapeHtml(timeStr)}</span>
        </div>
        <div class="chat-bubble-body">
          ${isUser ? escapeHtml(msg.text).replace(/\n/g, '<br>') : renderMarkdown(msg.text)}
        </div>
        ${actionsHtml}
      </div>
    `;
  }).join('');
  
  container.scrollTop = container.scrollHeight;
}

function onChatActionButtonClick(btnEl) {
  const action = btnEl.dataset.action;
  const target = btnEl.dataset.target;
  const targetVersion = btnEl.dataset.version;
  const isCluster = btnEl.dataset.cluster === 'true';
  let params = null;
  try {
    if (btnEl.dataset.params) params = JSON.parse(btnEl.dataset.params);
  } catch (e) {}
  triggerChatAction(action, target, targetVersion, isCluster, params);
}

async function sendChatMessage() {
  const input = document.getElementById('chatInput');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';

  const tempUserMsg = {
    id: 'msg_temp_' + Date.now(),
    sender: 'user',
    user_name: currentUserProfile.name,
    user_role: currentUserProfile.role,
    avatar: currentUserProfile.avatar,
    timestamp: new Date().toLocaleTimeString(),
    text: text,
    action_buttons: []
  };

  const container = document.getElementById('chatMessages');
  const tempDiv = document.createElement('div');
  tempDiv.id = tempUserMsg.id;
  tempDiv.className = 'message-bubble message-user';
  tempDiv.innerHTML = `
    <div class="chat-bubble-header">
      <div class="chat-bubble-user-info">
        <span>${escapeHtml(tempUserMsg.avatar)}</span>
        <span>${escapeHtml(tempUserMsg.user_name)}</span>
        <span class="val-badge info" style="font-size: 0.65rem; padding: 1px 5px;">${escapeHtml(tempUserMsg.user_role)}</span>
      </div>
      <span class="chat-bubble-time">${escapeHtml(tempUserMsg.timestamp)}</span>
    </div>
    <div class="chat-bubble-body">${escapeHtml(text)}</div>
  `;
  container.appendChild(tempDiv);

  const typingDiv = document.createElement('div');
  const typingId = 'typing_' + Date.now();
  typingDiv.id = typingId;
  typingDiv.className = 'message-bubble message-agent';
  typingDiv.innerHTML = `<em>🤖 RDS AI Agent is reasoning and executing resources...</em>`;
  container.appendChild(typingDiv);
  container.scrollTop = container.scrollHeight;

  try {
    const res = await fetch('/api/chat/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: text,
        session_id: activeSessionId,
        user_name: currentUserProfile.name,
        user_role: currentUserProfile.role,
        avatar: currentUserProfile.avatar
      })
    });
    const data = await res.json();
    
    if (data.messages) {
      renderAllChatMessages(data.messages);
    }
    
    // Refresh sessions list to update message counts / auto-title
    fetch('/api/chat/sessions').then(r => r.json()).then(sData => {
      if (sData.sessions) {
        currentChatSessions = sData.sessions;
        renderSessionsList();
        const activeSess = currentChatSessions.find(s => s.id === activeSessionId);
        if (activeSess) {
          const titleEl = document.getElementById('activeSessionTitle');
          if (titleEl) titleEl.textContent = activeSess.title;
        }
      }
    }).catch(err => console.warn('Could not refresh sessions', err));

    // Refresh fleet and tasks in case operational actions occurred
    fetchFleet();
    fetchUpgradeTasks();
  } catch (e) {
    typingDiv.innerHTML = `<span style="color:#ef4444;">Error interacting with AI agent: ${e.message}</span>`;
  }
}

async function triggerChatAction(action, target, targetVersion, isCluster, params = null) {
  const container = document.getElementById('chatMessages');
  const typingDiv = document.createElement('div');
  typingDiv.className = 'message-bubble message-agent';
  typingDiv.innerHTML = `<em>⚡ Executing action: <strong>${escapeHtml(action)}</strong> on <code>${escapeHtml(target)}</code>...</em>`;
  container.appendChild(typingDiv);
  container.scrollTop = container.scrollHeight;

  try {
    const res = await fetch('/api/chat/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: action,
        target: target,
        target_version: targetVersion || null,
        is_cluster: isCluster || false,
        session_id: activeSessionId,
        params: params || null
      })
    });
    const data = await res.json();
    
    if (data.messages) {
      renderAllChatMessages(data.messages);
    }
    
    fetchFleet();
    fetchUpgradeTasks();
  } catch (e) {
    typingDiv.innerHTML = `<span style="color:#ef4444;">Failed to execute action: ${e.message}</span>`;
  }
}

async function clearPersistentChat() {
  if (!confirm('Are you sure you want to clear this chat thread?')) return;
  try {
    const res = await fetch(`/api/chat/clear?session_id=${encodeURIComponent(activeSessionId)}`, { method: 'POST' });
    const data = await res.json();
    renderAllChatMessages(data.messages || []);
    loadChatSessions();
  } catch (e) {
    alert('Failed to clear chat: ' + e.message);
  }
}

// User Profile Modal
function openProfileModal() {
  document.getElementById('profileNameInput').value = currentUserProfile.name || '';
  document.getElementById('profileRoleInput').value = currentUserProfile.role || '';
  document.getElementById('profileAvatarSelect').value = currentUserProfile.avatar || '👨‍💻';
  openModal('profileModal');
}

async function saveProfile() {
  const name = document.getElementById('profileNameInput').value.trim();
  const role = document.getElementById('profileRoleInput').value.trim();
  const avatar = document.getElementById('profileAvatarSelect').value;

  try {
    const res = await fetch('/api/chat/user', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, role, avatar })
    });
    const data = await res.json();
    if (data.user_profile) {
      currentUserProfile = data.user_profile;
      updateUserProfileHeader();
    }
    closeModal('profileModal');
  } catch (e) {
    alert('Failed to save profile: ' + e.message);
  }
}

// Rename Modal Handlers
function openRenameModal(dbId) {
  const select = document.getElementById('renameDbSelect');
  if (select && currentFleet) {
    select.innerHTML = currentFleet.map(d => `<option value="${escapeHtml(d.id)}" ${d.id === dbId ? 'selected' : ''}>${escapeHtml(d.id)} (${d.type})</option>`).join('');
  }
  document.getElementById('renameNewIdInput').value = `${dbId}-new`;
  openModal('renameModal');
}

async function executeRenameDb() {
  const oldId = document.getElementById('renameDbSelect').value;
  const newId = document.getElementById('renameNewIdInput').value.trim();
  if (!oldId || !newId) {
    alert('Please enter a valid new database identifier');
    return;
  }

  const dbObj = currentFleet.find(d => d.id === oldId);
  const isCluster = dbObj ? dbObj.is_cluster : false;

  const btn = document.getElementById('confirmRenameBtn');
  btn.disabled = true;
  btn.textContent = '⏳ Renaming in AWS...';

  try {
    const res = await fetch('/api/db/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        old_identifier: oldId,
        new_identifier: newId,
        is_cluster: isCluster
      })
    });
    const data = await res.json();
    if (data.status === 'success') {
      closeModal('renameModal');
      fetchFleet();
      loadPersistentChat();
      alert(`Database successfully renamed from '${oldId}' to '${newId}'!`);
    } else {
      alert('Rename failed: ' + (data.message || 'Unknown error'));
    }
  } catch (e) {
    alert('Rename error: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '🔄 Rename Database';
  }
}

// Doomsday Rollback
function initDoomsday() {
  document.getElementById('executeDoomsdayBtn').addEventListener('click', async () => {
    const dbId = document.getElementById('doomsdayDbSelect').value;
    if (!dbId) return;

    if (!confirm(`Are you ABSOLUTELY sure you want to trigger Doomsday Rollback on '${dbId}'?`)) {
      return;
    }

    const term = document.getElementById('doomsdayTerminal');
    term.innerHTML = `<div class="log-line"><span class="log-time">[${new Date().toLocaleTimeString()}]</span><span class="log-msg-error">🚨 INITIATING DOOMSDAY ROLLBACK ON ${dbId}...</span></div>`;

    try {
      const res = await fetch('/api/doomsday/rollback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ db_identifier: dbId, is_cluster: true })
      });
      const data = await res.json();
      
      if (data.logs) {
        data.logs.forEach(l => {
          term.innerHTML += `<div class="log-line"><span class="log-time">[${new Date().toLocaleTimeString()}]</span><span class="log-msg-info">${escapeHtml(l)}</span></div>`;
        });
      }
      term.scrollTop = term.scrollHeight;
      fetchFleet();
      loadPersistentChat();
    } catch (e) {
      term.innerHTML += `<div class="log-line"><span class="log-time">[${new Date().toLocaleTimeString()}]</span><span class="log-msg-error">Rollback error: ${e.message}</span></div>`;
    }
  });
}

function updateDoomsdayDropdown(databases) {
  const select = document.getElementById('doomsdayDbSelect');
  if (!select) return;
  select.innerHTML = databases.map(d => `<option value="${escapeHtml(d.id)}">${escapeHtml(d.id)} (${d.type})</option>`).join('');
}

// Utility: Markdown and HTML escaping
function escapeHtml(text) {
  if (!text) return '';
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function renderMarkdown(md) {
  if (!md) return '';
  let html = md
    .replace(/### (.*?)\n/g, '<h4 style="margin: 0.75rem 0 0.4rem; color: var(--accent-cyan);">$1</h4>')
    .replace(/#### (.*?)\n/g, '<h5 style="margin: 0.5rem 0 0.3rem; color: var(--text-main);">$1</h5>')
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.*?)\*/g, '<em>$1</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\n\n/g, '<br><br>')
    .replace(/\n/g, '<br>');

  // Simple Markdown Table Converter
  if (html.includes('|')) {
    const lines = md.split('\n');
    let inTable = false;
    let tableHtml = '<table style="width:100%; border-collapse: collapse; margin: 10px 0;">';
    
    lines.forEach(l => {
      if (l.trim().startsWith('|') && l.trim().endsWith('|')) {
        inTable = true;
        const cells = l.split('|').filter((_, idx, arr) => idx > 0 && idx < arr.length - 1);
        if (l.includes(':---') || l.includes('---')) return; // separator
        
        tableHtml += '<tr>' + cells.map(c => `<td style="border:1px solid rgba(255,255,255,0.1); padding:5px 8px;">${c.trim()}</td>`).join('') + '</tr>';
      }
    });
    tableHtml += '</table>';
    if (inTable) {
      html = html.replace(/\|.*\|/s, tableHtml);
    }
  }

  return html;
}

