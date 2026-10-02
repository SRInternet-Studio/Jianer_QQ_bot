<script setup>
import { computed, nextTick, reactive, ref } from "vue";
import {
  AlertCircle,
  ArrowRight,
  ArrowUpRight,
  Brain,
  Check,
  ChevronLeft,
  ChevronRight,
  CircleDot,
  Database,
  Eye,
  FileText,
  LayoutDashboard,
  ListFilter,
  LoaderCircle,
  MessagesSquare,
  Network,
  Plug,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldCheck,
  Trash2,
  UserRound,
  Wrench,
  X,
} from "@lucide/vue";

const sections = [
  { key: "overview", label: "概览", icon: LayoutDashboard },
  { key: "search", label: "语义检索", icon: Search },
  { key: "paragraphs", label: "段落", icon: FileText },
  { key: "relations", label: "关系图", icon: Network },
  { key: "episodes", label: "对话片段", icon: MessagesSquare },
  { key: "profiles", label: "人物画像", icon: UserRound },
  { key: "facts", label: "事实", icon: CircleDot },
  { key: "sources", label: "来源", icon: Database },
  { key: "recycle-bin", label: "回收站", icon: Trash2 },
  { key: "maintenance", label: "维护任务", icon: Wrench },
];

const resourceLabels = {
  paragraphs: "段落",
  relations: "关系",
  episodes: "对话片段",
  profiles: "人物画像",
  facts: "事实",
  sources: "来源",
  "recycle-bin": "回收站记录",
};

const preferredFields = {
  paragraphs: ["hash", "content", "source", "knowledge_type", "created_at"],
  episodes: ["episode_id", "source", "summary", "created_at"],
  relations: ["subject", "predicate", "object", "weight", "created_at"],
  profiles: ["person_id", "updated_at", "created_at"],
  facts: ["person_id", "subject", "predicate", "object", "confidence", "created_at"],
  sources: ["source_id", "title", "source_type", "url", "created_at"],
  "recycle-bin": ["operation_id", "mode", "status", "created_at"],
};

const fieldLabels = {
  hash: "记录 ID",
  content: "内容",
  source: "来源",
  source_id: "来源 ID",
  source_type: "来源类型",
  knowledge_type: "类型",
  created_at: "创建时间",
  updated_at: "更新时间",
  episode_id: "片段 ID",
  summary: "摘要",
  subject: "主体",
  predicate: "关系",
  object: "客体",
  weight: "权重",
  person_id: "人物 ID",
  confidence: "置信度",
  operation_id: "操作 ID",
  mode: "操作类型",
  status: "状态",
  title: "标题",
  url: "地址",
};

const modeOptions = [
  { value: "search", label: "默认" },
  { value: "hybrid", label: "混合检索" },
  { value: "time", label: "时间检索" },
  { value: "episode", label: "对话片段" },
  { value: "aggregate", label: "聚合检索" },
];

const profileSectionTitles = [
  "身份设定",
  "关系设定",
  "稳定了解",
  "相处偏好",
  "近期互动",
  "不确定信息",
  "维护备注",
];

const pageSize = 50;
const currentView = ref("overview");
const currentSection = computed(() => sections.find((item) => item.key === currentView.value) || sections[0]);
const token = ref("");
const tokenDraft = ref("");
const tokenDialog = ref(null);
const tokenError = ref("");
const connectionState = ref("disconnected");
const connectionError = ref("");
const isRefreshing = ref(false);
const updatedAt = ref("");
const statsData = ref(null);
const embeddingData = ref(null);
const searchQuery = ref("");
const searchMode = ref("search");
const searchLoading = ref(false);
const searchError = ref("");
const searchData = ref(null);
const graphQuery = ref("");
const graphLoading = ref(false);
const graphError = ref("");
const graphData = ref(null);
const profileId = ref("");
const profileLoading = ref(false);
const profileError = ref("");
const profileData = ref(null);
const recordDialog = ref(null);
const selectedRecord = ref(null);
const resourceFilter = ref("");
const lifecycleResult = ref(null);
const maintenanceResult = ref(null);
const maintenanceForm = reactive({ action: "status", target: "", hours: "", reason: "console" });
const deleteForm = reactive({ mode: "paragraph", selector: "" });
const restoreOperationId = ref("");

const resourceState = reactive(Object.fromEntries(
  Object.keys(resourceLabels).map((key) => [key, { items: [], loading: false, error: "", offset: 0 }]),
));

const metricSpecs = [
  ["paragraphs", "段落"],
  ["relations", "关系"],
  ["episodes", "对话片段"],
  ["profiles", "人物画像"],
  ["paragraph_vector_backfill_pending", "待补向量"],
  ["profile_refresh_pending", "待刷新画像"],
];

const metrics = computed(() => {
  const data = statsData.value?.stats || {};
  return metricSpecs.map(([key, label]) => ({ key, label, value: formatNumber(data[key]) }));
});

const vectorPools = computed(() => {
  const pools = embeddingData.value?.vector_pools;
  return pools && typeof pools === "object" ? Object.entries(pools) : [];
});

const embeddingLabel = computed(() => {
  if (!embeddingData.value) return "待连接";
  if (embeddingData.value.degraded?.active) return "降级";
  return embeddingData.value.available ? "可用" : "不可用";
});

const embeddingDimension = computed(() => {
  const dimension = embeddingData.value?.dimension;
  if (typeof dimension === "number" || typeof dimension === "string") return dimension;
  return dimension?.dimension ?? dimension?.value ?? dimension?.effective_dimension ?? "—";
});

const embeddingModel = computed(() => {
  const fingerprint = embeddingData.value?.fingerprint;
  return fingerprint?.model || fingerprint?.model_name || "—";
});

const searchHits = computed(() => {
  const data = searchData.value;
  if (Array.isArray(data?.hits)) return data.hits;
  if (Array.isArray(data?.items)) return data.items;
  return [];
});

const currentResource = computed(() => resourceState[currentView.value] || null);

const resourceFields = computed(() => {
  const rows = currentResource.value?.items || [];
  if (!rows.length) return [];
  const preferred = preferredFields[currentView.value] || [];
  const present = preferred.filter((field) => rows.some((row) => field in row));
  const extra = Object.keys(rows[0]).filter((field) => !present.includes(field)).slice(0, Math.max(0, 6 - present.length));
  return [...present, ...extra];
});

const filteredResourceItems = computed(() => {
  const rows = currentResource.value?.items || [];
  const query = resourceFilter.value.trim().toLocaleLowerCase();
  if (!query) return rows;
  return rows.filter((row) => JSON.stringify(row).toLocaleLowerCase().includes(query));
});

const graphLayout = computed(() => {
  if (!graphData.value) return { nodes: [], edges: [] };
  const rawEdges = Array.isArray(graphData.value.edges)
    ? graphData.value.edges
    : (Array.isArray(graphData.value.relations) ? graphData.value.relations : []);
  const rawNodes = Array.isArray(graphData.value.nodes) ? graphData.value.nodes : [];
  const nodeMap = new Map();

  for (const node of rawNodes) {
    const id = graphNodeId(node);
    if (id) nodeMap.set(id, { id, label: graphNodeLabel(node), raw: node });
  }
  for (const edge of rawEdges) {
    for (const endpoint of [graphEndpoint(edge, "source"), graphEndpoint(edge, "target")]) {
      const id = graphNodeId(endpoint);
      if (id && !nodeMap.has(id)) nodeMap.set(id, { id, label: graphNodeLabel(endpoint), raw: endpoint });
    }
  }

  const nodes = [...nodeMap.values()].slice(0, 18).map((node, index, all) => {
    const angle = (2 * Math.PI * index) / Math.max(all.length, 1) - Math.PI / 2;
    return { ...node, x: 500 + Math.cos(angle) * 350, y: 265 + Math.sin(angle) * 205 };
  });
  const positions = new Map(nodes.map((node) => [node.id, node]));
  const edges = rawEdges.slice(0, 60).flatMap((edge) => {
    const source = positions.get(graphNodeId(graphEndpoint(edge, "source")));
    const target = positions.get(graphNodeId(graphEndpoint(edge, "target")));
    if (!source || !target) return [];
    return [{ source, target, label: String(edge.predicate ?? edge.label ?? edge.type ?? "") }];
  });
  return { nodes, edges, totalNodes: nodeMap.size, totalEdges: rawEdges.length };
});

const profileSections = computed(() => {
  const source = profileData.value?.sections;
  return profileSectionTitles.map((title) => ({
    title,
    items: Array.isArray(source?.[title]) ? source[title] : [],
  }));
});

const profileAliases = computed(() => {
  const aliases = profileData.value?.aliases;
  return Array.isArray(aliases) ? aliases.filter((item) => String(item || "").trim()) : [];
});

const profileEvidence = computed(() => (
  Array.isArray(profileData.value?.evidence) ? profileData.value.evidence : []
));

const isConnected = computed(() => connectionState.value === "connected");
const connectionLabel = computed(() => ({
  disconnected: "未连接",
  connecting: "连接中",
  connected: "已连接",
  error: "连接失败",
}[connectionState.value] || "未连接"));

function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number") return value.toLocaleString("zh-CN");
  return String(value);
}

function formatValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

function fieldLabel(field) {
  return fieldLabels[field] || field;
}

function displayFieldValue(field, value) {
  return field.endsWith("_at") ? formatDate(value) : formatValue(value);
}

function graphNodeId(value) {
  if (value && typeof value === "object") {
    return String(value.id ?? value.node_id ?? value.name ?? value.label ?? value.entity ?? "");
  }
  return String(value ?? "");
}

function graphNodeLabel(value) {
  if (value && typeof value === "object") {
    return String(value.name ?? value.label ?? value.entity ?? value.text ?? value.id ?? value.node_id ?? "");
  }
  return String(value ?? "");
}

function graphEndpoint(edge, side) {
  return side === "source"
    ? (edge.subject ?? edge.source ?? edge.from ?? edge.source_id ?? "")
    : (edge.object ?? edge.target ?? edge.to ?? edge.target_id ?? "");
}

function jsonText(value) {
  return JSON.stringify(value ?? {}, null, 2);
}

function profileItemText(value) {
  return String(value || "").replace(/^\s*-\s*/, "").trim();
}

function profileItemIsPending(value) {
  return profileItemText(value).startsWith("待确认：");
}

async function api(path, options = {}) {
  if (window.location.protocol === "file:") {
    throw new Error("请通过机器人启动日志中的 HTTP 控制台地址访问，直接打开 HTML 文件无法连接记忆 API。");
  }
  const headers = new Headers(options.headers || {});
  headers.set("X-Memory-Token", token.value);
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(`/api${path}`, { ...options, headers });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body.detail || body.error || `HTTP ${response.status}`;
    if (response.status === 401) {
      token.value = "";
      connectionState.value = "error";
      throw new Error("Token 无效或已过期，请重新连接。");
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

function openTokenDialog() {
  tokenError.value = "";
  tokenDraft.value = "";
  tokenDialog.value?.showModal();
}

async function connect() {
  const value = tokenDraft.value.trim();
  if (!value) {
    tokenError.value = "请输入本次启动时生成的 Token。";
    return;
  }
  token.value = value;
  tokenDraft.value = "";
  tokenError.value = "";
  connectionState.value = "connecting";
  tokenDialog.value?.close();
  await refreshOverview();
}

async function refreshOverview() {
  if (!token.value) {
    connectionState.value = "disconnected";
    connectionError.value = "请输入 Token 后连接控制台。";
    return;
  }
  isRefreshing.value = true;
  connectionError.value = "";
  try {
    const [health, stats, embedding] = await Promise.all([
      api("/health"),
      api("/stats"),
      api("/embedding/status"),
    ]);
    statsData.value = stats;
    embeddingData.value = embedding;
    connectionState.value = "connected";
    connectionError.value = health.ready ? "记忆运行时已就绪。" : "记忆运行时尚未就绪。";
    updatedAt.value = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  } catch (error) {
    connectionState.value = "error";
    connectionError.value = error.message || String(error);
  } finally {
    isRefreshing.value = false;
  }
}

async function loadResource(resource, offset = resourceState[resource].offset) {
  if (!token.value) return;
  const state = resourceState[resource];
  state.loading = true;
  state.error = "";
  state.offset = Math.max(0, offset);
  try {
    const params = new URLSearchParams({ limit: String(pageSize), offset: String(state.offset) });
    const payload = await api(`/${resource}?${params}`);
    state.items = Array.isArray(payload.items) ? payload.items : [];
    if (payload.error) state.error = payload.error;
    resourceFilter.value = "";
  } catch (error) {
    state.error = error.message || String(error);
  } finally {
    state.loading = false;
  }
}

async function selectSection(section) {
  currentView.value = section;
  resourceFilter.value = "";
  if (!token.value) return;
  if (resourceState[section] && section !== "relations") await loadResource(section, 0);
  if (section === "relations") await loadGraph();
}

async function submitSearch() {
  if (!searchQuery.value.trim()) return;
  searchLoading.value = true;
  searchError.value = "";
  try {
    const params = new URLSearchParams({ q: searchQuery.value.trim(), mode: searchMode.value });
    searchData.value = await api(`/search?${params}`);
  } catch (error) {
    searchError.value = error.message || String(error);
  } finally {
    searchLoading.value = false;
  }
}

async function loadGraph() {
  if (!token.value) return;
  graphLoading.value = true;
  graphError.value = "";
  try {
    const params = new URLSearchParams({ q: graphQuery.value.trim(), limit: "200" });
    graphData.value = await api(`/graph?${params}`);
    if (graphData.value.error) graphError.value = graphData.value.error;
  } catch (error) {
    graphError.value = error.message || String(error);
  } finally {
    graphLoading.value = false;
  }
}

async function loadProfile() {
  if (!profileId.value.trim()) return;
  profileLoading.value = true;
  profileError.value = "";
  profileData.value = null;
  try {
    profileData.value = await api(`/profile/${encodeURIComponent(profileId.value.trim())}`);
    if (profileData.value.error) profileError.value = profileData.value.error;
  } catch (error) {
    profileError.value = error.message || String(error);
  } finally {
    profileLoading.value = false;
  }
}

async function restoreMemory() {
  if (!restoreOperationId.value.trim()) return;
  try {
    lifecycleResult.value = await api("/restore", {
      method: "POST",
      body: JSON.stringify({ operation_id: restoreOperationId.value.trim(), requested_by: "console" }),
    });
    await loadResource("recycle-bin");
  } catch (error) {
    lifecycleResult.value = { error: error.message || String(error) };
  }
}

async function previewDelete() {
  if (!deleteForm.selector.trim()) return;
  try {
    lifecycleResult.value = await api("/delete", {
      method: "POST",
      body: JSON.stringify({
        action: "preview",
        mode: deleteForm.mode,
        selector: { query: deleteForm.selector.trim() },
        requested_by: "console",
      }),
    });
  } catch (error) {
    lifecycleResult.value = { error: error.message || String(error) };
  }
}

async function submitMaintenance() {
  const payload = {
    ...maintenanceForm,
    target: maintenanceForm.target.trim(),
    hours: maintenanceForm.hours === "" ? null : Number(maintenanceForm.hours),
  };
  try {
    maintenanceResult.value = await api("/maintenance", { method: "POST", body: JSON.stringify(payload) });
  } catch (error) {
    maintenanceResult.value = { error: error.message || String(error) };
  }
}

function openRecord(record) {
  selectedRecord.value = record;
  nextTick(() => recordDialog.value?.showModal());
}

function formatScore(hit) {
  const score = hit.score ?? hit.similarity;
  if (typeof score === "number") return score.toFixed(4);
  return score ?? "—";
}
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <a class="brand" href="./" aria-label="JianerAI 记忆控制台">
        <Brain :size="21" :stroke-width="1.8" aria-hidden="true" />
        <span class="brand-name">JianerAI</span>
        <span class="brand-divider" aria-hidden="true"></span>
        <span class="brand-product">记忆</span>
      </a>
      <div class="topbar-tools">
        <div class="connection-state" :class="`state-${connectionState}`" role="status" aria-live="polite">
          <span class="connection-dot"></span>
          <span>{{ connectionLabel }}</span>
        </div>
        <button class="icon-button" type="button" title="刷新运行状态" aria-label="刷新运行状态" :disabled="!token || isRefreshing" @click="refreshOverview">
          <LoaderCircle v-if="isRefreshing" :size="17" class="spin" />
          <RefreshCw v-else :size="17" />
        </button>
        <button class="connect-button" type="button" :disabled="connectionState === 'connecting'" @click="openTokenDialog">
          <Plug :size="16" aria-hidden="true" />
          <span>{{ isConnected ? "重新连接" : "连接控制台" }}</span>
        </button>
      </div>
    </header>

    <div class="workspace">
      <aside class="sidebar" aria-label="记忆控制台导航">
        <div class="sidebar-heading">工作区</div>
        <nav class="section-nav">
          <button
            v-for="(section, index) in sections"
            :key="section.key"
            class="nav-item"
            :class="{ active: currentView === section.key }"
            type="button"
            :aria-current="currentView === section.key ? 'page' : undefined"
            @click="selectSection(section.key)"
          >
            <span class="nav-index">{{ String(index + 1).padStart(2, "0") }}</span>
            <component :is="section.icon" :size="17" :stroke-width="1.8" aria-hidden="true" />
            <span class="nav-label">{{ section.label }}</span>
          </button>
        </nav>
        <div class="sidebar-status">
          <div class="sidebar-status-label">运行状态</div>
          <div class="sidebar-status-value" :class="`state-${connectionState}`">
            <span class="connection-dot"></span>{{ connectionLabel }}
          </div>
          <div class="token-note">令牌仅保存在当前页面内存中</div>
        </div>
      </aside>

      <main class="main-content">
        <div class="page-heading">
          <div class="folio-index">{{ String(sections.findIndex((item) => item.key === currentView) + 1).padStart(2, "0") }}</div>
          <div class="heading-copy"><h1>{{ currentSection.label }}</h1></div>
          <div class="heading-meta" v-if="updatedAt && currentView === 'overview'">更新于 {{ updatedAt }}</div>
        </div>

        <div v-if="connectionError" class="runtime-notice" :class="{ 'notice-error': connectionState === 'error' }" role="status">
          <AlertCircle v-if="connectionState === 'error'" :size="17" aria-hidden="true" />
          <Check v-else :size="17" aria-hidden="true" />
          <span>{{ connectionError }}</span>
          <button v-if="!isConnected" class="text-button" type="button" @click="openTokenDialog">连接</button>
        </div>

        <section v-if="currentView === 'overview'" class="view-content">
          <div class="section-bar">
            <h2>记忆数据</h2>
            <span v-if="statsData?.backend" class="section-meta">{{ statsData.backend }}</span>
          </div>
          <div class="metric-grid" :aria-busy="isRefreshing">
            <div v-for="metric in metrics" :key="metric.key" class="metric-cell">
              <div class="metric-label">{{ metric.label }}</div>
              <div class="metric-value">{{ metric.value }}</div>
            </div>
          </div>

          <div class="overview-grid">
            <section class="data-section">
              <div class="section-bar">
                <h2>Embedding</h2>
                <span class="status-tag" :class="embeddingData?.degraded?.active ? 'tag-warning' : (embeddingData?.available ? 'tag-success' : 'tag-muted')">
                  {{ embeddingLabel }}
                </span>
              </div>
              <dl class="definition-grid">
                <div><dt>模型</dt><dd>{{ embeddingModel }}</dd></div>
                <div><dt>向量维度</dt><dd>{{ embeddingDimension }}</dd></div>
                <div><dt>运行时</dt><dd>{{ embeddingData?.state || "—" }}</dd></div>
                <div><dt>缓存状态</dt><dd>{{ embeddingData?.degraded?.active ? "降级中" : (embeddingData ? "正常" : "—") }}</dd></div>
              </dl>
              <details v-if="embeddingData" class="raw-details">
                <summary>Embedding 诊断数据</summary>
                <pre>{{ jsonText(embeddingData) }}</pre>
              </details>
              <div v-else class="empty-line">连接后显示 Embedding 状态。</div>
            </section>

            <section class="data-section">
              <div class="section-bar"><h2>向量池</h2><span class="section-meta">{{ vectorPools.length }} 个</span></div>
              <div v-if="vectorPools.length" class="pool-table">
                <div v-for="[name, value] in vectorPools" :key="name" class="pool-row">
                  <span>{{ name }}</span><span>{{ formatValue(value) }}</span>
                </div>
              </div>
              <div v-else class="empty-line">{{ embeddingData ? "暂无向量池数据。" : "连接后显示向量池。" }}</div>
            </section>
          </div>
        </section>

        <section v-else-if="currentView === 'search'" class="view-content">
          <form class="search-form" @submit.prevent="submitSearch">
            <label class="field-label" for="search-query">查询文本</label>
            <input id="search-query" v-model="searchQuery" class="text-input search-input" autocomplete="off" required />
            <label class="visually-hidden" for="search-mode">检索模式</label>
            <select id="search-mode" v-model="searchMode" class="select-input">
              <option v-for="mode in modeOptions" :key="mode.value" :value="mode.value">{{ mode.label }}</option>
            </select>
            <button class="primary-button" type="submit" :disabled="searchLoading || !isConnected">
              <LoaderCircle v-if="searchLoading" :size="16" class="spin" />
              <Search v-else :size="16" />
              <span>执行检索</span>
            </button>
          </form>
          <div v-if="searchError" class="inline-error" role="alert">{{ searchError }}</div>
          <div v-if="searchData" class="result-heading">
            <span>{{ searchData.summary || `${searchHits.length} 条命中` }}</span>
            <span v-if="searchData.retrieval_mode" class="section-meta">{{ searchData.retrieval_mode }}</span>
          </div>
          <div v-if="searchLoading" class="empty-state">正在检索</div>
          <div v-else-if="searchData && !searchHits.length" class="empty-state">没有命中记录。</div>
          <div v-else-if="!searchData" class="empty-state">{{ isConnected ? "输入查询文本后执行检索。" : "连接控制台后可检索记忆。" }}</div>
          <div v-else class="search-results">
            <article v-for="(hit, index) in searchHits" :key="hit.hash || hit.id || index" class="search-result">
              <div class="result-index">{{ String(index + 1).padStart(2, "0") }}</div>
              <div class="result-body">
                <div class="result-topline"><span>{{ hit.type || hit.knowledge_type || "记忆" }}</span><span>{{ formatScore(hit) }}</span></div>
                <p>{{ hit.content || hit.text || hit.summary || formatValue(hit) }}</p>
                <div class="result-source">{{ hit.source || hit.hash || hit.paragraph_hash || "" }}</div>
              </div>
            </article>
          </div>
        </section>

        <section v-else-if="resourceState[currentView]" class="view-content">
          <div v-if="currentView === 'relations'" class="graph-tools">
            <form class="search-form graph-search" @submit.prevent="loadGraph">
              <label class="field-label" for="graph-query">实体或关系</label>
              <input id="graph-query" v-model="graphQuery" class="text-input search-input" />
              <button class="primary-button" type="submit" :disabled="graphLoading || !isConnected">
                <LoaderCircle v-if="graphLoading" :size="16" class="spin" />
                <Search v-else :size="16" />
                <span>查询图谱</span>
              </button>
            </form>
            <div v-if="graphError" class="inline-error" role="alert">{{ graphError }}</div>
            <div v-if="graphLayout.totalNodes" class="graph-summary">
              <span>{{ graphLayout.totalNodes }} 个节点</span>
              <span>{{ graphLayout.totalEdges }} 条关系</span>
            </div>
            <div v-if="graphLayout.nodes.length" class="graph-frame" role="img" aria-label="关系图谱">
              <svg viewBox="0 0 1000 530" preserveAspectRatio="xMidYMid meet">
                <defs>
                  <marker id="graph-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                    <path d="M0,0 L8,4 L0,8" fill="none" stroke="#81858B" stroke-width="1.2" />
                  </marker>
                </defs>
                <g class="graph-edges">
                  <line v-for="(edge, index) in graphLayout.edges" :key="index" :x1="edge.source.x" :y1="edge.source.y" :x2="edge.target.x" :y2="edge.target.y" marker-end="url(#graph-arrow)" />
                  <text v-for="(edge, index) in graphLayout.edges" :key="`label-${index}`" :x="(edge.source.x + edge.target.x) / 2" :y="(edge.source.y + edge.target.y) / 2 - 7">{{ edge.label }}</text>
                </g>
                <g v-for="node in graphLayout.nodes" :key="node.id" class="graph-node" :transform="`translate(${node.x}, ${node.y})`">
                  <title>{{ node.label }}</title>
                  <circle r="22" />
                  <text y="42">{{ node.label.length > 12 ? `${node.label.slice(0, 12)}…` : node.label }}</text>
                </g>
              </svg>
            </div>
            <div v-else class="empty-state">{{ isConnected ? "没有可显示的关系。" : "连接控制台后查看关系图。" }}</div>
          </div>

          <template v-else-if="currentView === 'profiles'">
            <form class="search-form profile-form" @submit.prevent="loadProfile">
              <label class="field-label" for="profile-id">人物 ID</label>
              <input id="profile-id" v-model="profileId" class="text-input search-input" required />
              <button class="primary-button" type="submit" :disabled="profileLoading || !isConnected">
                <LoaderCircle v-if="profileLoading" :size="16" class="spin" />
                <Eye v-else :size="16" />
                <span>读取画像</span>
              </button>
            </form>
            <div v-if="profileError" class="inline-error" role="alert">{{ profileError }}</div>
            <div v-if="profileData?.success" class="profile-workbench">
              <section class="profile-hero" aria-label="人物画像摘要">
                <div class="profile-hero-mark"><UserRound :size="25" :stroke-width="1.6" /></div>
                <div class="profile-hero-copy">
                  <div class="profile-kicker">自动人物画像</div>
                  <h2>{{ profileData.person_name || profileData.person_id }}</h2>
                  <p>{{ profileData.profile_source === "manual_override" ? "当前展示的是手工覆盖版本。" : "由事实、关系和对话证据自动整理。" }}</p>
                </div>
                <span class="status-tag" :class="profileData.has_manual_override ? 'tag-warning' : 'tag-success'">
                  {{ profileData.has_manual_override ? "手工覆盖" : "自动生成" }}
                </span>
              </section>

              <dl class="profile-meta-grid">
                <div><dt>人物 ID</dt><dd>{{ profileData.person_id || "—" }}</dd></div>
                <div><dt>别名</dt><dd>{{ profileAliases.length ? profileAliases.join("、") : "—" }}</dd></div>
                <div><dt>画像版本</dt><dd>{{ profileData.profile_version ?? "—" }}</dd></div>
                <div><dt>更新时间</dt><dd>{{ formatDate(profileData.updated_at) }}</dd></div>
                <div><dt>证据条数</dt><dd>{{ profileData.evidence_count ?? profileEvidence.length }}</dd></div>
                <div><dt>待确认事实</dt><dd>{{ profileData.uncertain_fact_count ?? "—" }}</dd></div>
              </dl>

              <section class="profile-sections" aria-label="画像段落">
                <div class="section-bar"><h2>画像内容</h2><span class="section-meta">空白段落不会填充虚构内容</span></div>
                <div class="profile-section-grid">
                  <article v-for="section in profileSections" :key="section.title" class="profile-section-block" :class="{ 'profile-section-empty': !section.items.some((item) => profileItemText(item) && profileItemText(item) !== '暂无') }">
                    <div class="profile-section-heading">
                      <h3>{{ section.title }}</h3>
                      <span>{{ section.items.filter((item) => profileItemText(item) && profileItemText(item) !== '暂无').length }} 条</span>
                    </div>
                    <ul v-if="section.items.some((item) => profileItemText(item) && profileItemText(item) !== '暂无')" class="profile-items">
                      <li v-for="(item, index) in section.items" v-show="profileItemText(item) && profileItemText(item) !== '暂无'" :key="`${section.title}-${index}`" :class="{ 'profile-item-pending': profileItemIsPending(item) }">
                        <span class="profile-item-bullet" aria-hidden="true"></span>
                        <span>{{ profileItemText(item) }}</span>
                      </li>
                    </ul>
                    <div v-else class="profile-section-placeholder">暂无已确认内容</div>
                  </article>
                </div>
              </section>

              <section class="profile-evidence" aria-label="画像证据">
                <div class="section-bar"><h2>证据链</h2><span class="section-meta">{{ profileEvidence.length }} 条可见证据</span></div>
                <div v-if="profileEvidence.length" class="profile-evidence-list">
                  <article v-for="(item, index) in profileEvidence" :key="item.hash || index" class="profile-evidence-row">
                    <div class="profile-evidence-index">{{ String(index + 1).padStart(2, "0") }}</div>
                    <div class="profile-evidence-body">
                      <div class="profile-evidence-topline"><span>{{ item.type === "relation" ? "关系" : "段落" }}</span><span>{{ item.metadata?.confidence != null ? `置信度 ${Number(item.metadata.confidence).toFixed(2)}` : "" }}</span></div>
                      <p>{{ item.content || "—" }}</p>
                      <code>{{ item.hash || "—" }}</code>
                    </div>
                  </article>
                </div>
                <div v-else class="empty-line">暂无可见证据。</div>
              </section>

              <details class="raw-details">
                <summary>查看原始数据</summary>
                <pre>{{ jsonText(profileData) }}</pre>
              </details>
            </div>
            <div v-else-if="profileData" class="empty-state">{{ profileData.error || "没有读取到人物画像。" }}</div>
            <div v-else class="empty-state">{{ isConnected ? "输入人物 ID 读取画像。" : "连接控制台后读取人物画像。" }}</div>
            <section class="profile-index">
              <div class="section-bar"><h2>人物画像索引</h2><span class="section-meta">{{ resourceState.profiles.items.length }} 条</span></div>
              <div v-if="resourceState.profiles.error" class="inline-error">{{ resourceState.profiles.error }}</div>
              <div v-else-if="resourceState.profiles.loading" class="empty-state">正在读取人物画像</div>
              <div v-else-if="!resourceState.profiles.items.length" class="empty-state">{{ isConnected ? "暂无人物画像记录。" : "连接控制台后查看画像索引。" }}</div>
              <div v-else class="table-scroll">
                <table class="records-table">
                  <thead><tr><th v-for="field in resourceFields" :key="field">{{ fieldLabel(field) }}</th><th class="action-header">详情</th></tr></thead>
                  <tbody>
                    <tr v-for="(row, index) in resourceState.profiles.items" :key="row.person_id || index">
                      <td v-for="field in resourceFields" :key="field">{{ displayFieldValue(field, row[field]) }}</td>
                      <td class="table-actions"><button class="icon-button row-detail-button" type="button" title="查看完整记录" aria-label="查看完整记录" @click="openRecord(row)"><ArrowUpRight :size="16" /></button></td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div v-if="resourceState.profiles.items.length" class="pagination">
                <span>第 {{ Math.floor(resourceState.profiles.offset / pageSize) + 1 }} 页</span>
                <button class="icon-button" type="button" title="上一页" aria-label="上一页" :disabled="resourceState.profiles.offset === 0 || resourceState.profiles.loading" @click="loadResource('profiles', resourceState.profiles.offset - pageSize)"><ChevronLeft :size="17" /></button>
                <button class="icon-button" type="button" title="下一页" aria-label="下一页" :disabled="resourceState.profiles.items.length < pageSize || resourceState.profiles.loading" @click="loadResource('profiles', resourceState.profiles.offset + pageSize)"><ChevronRight :size="17" /></button>
              </div>
            </section>
          </template>

          <template v-else-if="currentView === 'recycle-bin'">
            <div class="table-toolbar">
              <div class="toolbar-count">{{ resourceState['recycle-bin'].items.length }} 条记录</div>
              <button class="icon-button" type="button" title="刷新回收站" aria-label="刷新回收站" :disabled="resourceState['recycle-bin'].loading || !isConnected" @click="loadResource('recycle-bin')">
                <LoaderCircle v-if="resourceState['recycle-bin'].loading" :size="17" class="spin" />
                <RefreshCw v-else :size="17" />
              </button>
            </div>
            <div v-if="resourceState['recycle-bin'].error" class="inline-error">{{ resourceState['recycle-bin'].error }}</div>
            <div v-else-if="resourceState['recycle-bin'].loading" class="empty-state">正在读取回收站</div>
            <div v-else-if="!resourceState['recycle-bin'].items.length" class="empty-state">{{ isConnected ? "回收站为空。" : "连接控制台后查看回收站。" }}</div>
            <div v-else class="table-scroll">
              <table class="records-table">
                <thead><tr><th v-for="field in resourceFields" :key="field">{{ fieldLabel(field) }}</th><th>操作</th></tr></thead>
                <tbody>
                  <tr v-for="(row, index) in resourceState['recycle-bin'].items" :key="row.operation_id || index">
                    <td v-for="field in resourceFields" :key="field" :class="{ 'cell-content': field === 'summary' || field === 'content' }">{{ displayFieldValue(field, row[field]) }}</td>
                    <td class="table-actions"><button class="small-action" type="button" :disabled="!row.operation_id" @click="restoreOperationId = row.operation_id; restoreMemory()"><RotateCcw :size="14" /><span>恢复</span></button></td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="lifecycle-controls">
              <form class="restore-form" @submit.prevent="restoreMemory">
                <label class="field-label" for="restore-id">操作 ID</label>
                <input id="restore-id" v-model="restoreOperationId" class="text-input" required />
                <button class="secondary-button" type="submit" :disabled="!isConnected"><RotateCcw :size="15" /><span>恢复记录</span></button>
              </form>
              <form class="delete-form" @submit.prevent="previewDelete">
                <label class="field-label" for="delete-selector">删除目标</label>
                <select v-model="deleteForm.mode" class="select-input" aria-label="删除类型">
                  <option value="paragraph">段落</option><option value="relation">关系</option><option value="source">来源</option>
                </select>
                <input id="delete-selector" v-model="deleteForm.selector" class="text-input" required />
                <button class="secondary-button" type="submit" :disabled="!isConnected">预览删除</button>
              </form>
              <pre v-if="lifecycleResult" class="json-result result-span">{{ jsonText(lifecycleResult) }}</pre>
            </div>
          </template>

          <template v-else>
            <div class="table-toolbar">
              <label class="filter-field">
                <ListFilter :size="16" aria-hidden="true" />
                <span class="visually-hidden">筛选当前页</span>
                <input v-model="resourceFilter" class="filter-input" placeholder="筛选当前页" />
              </label>
              <div class="toolbar-actions">
                <span class="toolbar-count">{{ filteredResourceItems.length }} 条 / {{ resourceState[currentView].offset + 1 }}–{{ resourceState[currentView].offset + resourceState[currentView].items.length }}</span>
                <button class="icon-button" type="button" :title="`刷新${resourceLabels[currentView]}`" :aria-label="`刷新${resourceLabels[currentView]}`" :disabled="resourceState[currentView].loading || !isConnected" @click="loadResource(currentView)">
                  <LoaderCircle v-if="resourceState[currentView].loading" :size="17" class="spin" />
                  <RefreshCw v-else :size="17" />
                </button>
              </div>
            </div>
            <div v-if="resourceState[currentView].error" class="inline-error">{{ resourceState[currentView].error }}</div>
            <div v-else-if="resourceState[currentView].loading" class="empty-state">正在读取{{ resourceLabels[currentView] }}</div>
            <div v-else-if="!resourceState[currentView].items.length" class="empty-state">{{ isConnected ? `暂无${resourceLabels[currentView]}记录。` : `连接控制台后查看${resourceLabels[currentView]}。` }}</div>
            <div v-else-if="!filteredResourceItems.length" class="empty-state">当前页没有匹配记录。</div>
            <div v-else class="table-scroll">
              <table class="records-table">
                <thead><tr><th v-for="field in resourceFields" :key="field">{{ fieldLabel(field) }}</th><th class="action-header">详情</th></tr></thead>
                <tbody>
                  <tr v-for="(row, index) in filteredResourceItems" :key="row.hash || row.episode_id || row.source_id || row.person_id || index">
                    <td v-for="field in resourceFields" :key="field" :class="{ 'cell-content': field === 'summary' || field === 'content' }">{{ displayFieldValue(field, row[field]) }}</td>
                    <td class="table-actions"><button class="icon-button row-detail-button" type="button" title="查看完整记录" aria-label="查看完整记录" @click="openRecord(row)"><ArrowUpRight :size="16" /></button></td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-if="resourceState[currentView].items.length" class="pagination">
              <span>第 {{ Math.floor(resourceState[currentView].offset / pageSize) + 1 }} 页</span>
              <button class="icon-button" type="button" title="上一页" aria-label="上一页" :disabled="resourceState[currentView].offset === 0 || resourceState[currentView].loading" @click="loadResource(currentView, resourceState[currentView].offset - pageSize)"><ChevronLeft :size="17" /></button>
              <button class="icon-button" type="button" title="下一页" aria-label="下一页" :disabled="resourceState[currentView].items.length < pageSize || resourceState[currentView].loading" @click="loadResource(currentView, resourceState[currentView].offset + pageSize)"><ChevronRight :size="17" /></button>
            </div>
          </template>
        </section>

        <section v-else-if="currentView === 'maintenance'" class="view-content">
          <form class="maintenance-form" @submit.prevent="submitMaintenance">
            <label class="form-field"><span>动作</span><select v-model="maintenanceForm.action" class="select-input"><option value="status">查看状态</option><option value="run">运行</option><option value="prune">清理</option><option value="reinforce">强化</option><option value="protect">保护</option></select></label>
            <label class="form-field"><span>目标</span><input v-model="maintenanceForm.target" class="text-input" /></label>
            <label class="form-field"><span>时长（小时）</span><input v-model="maintenanceForm.hours" class="text-input" type="number" min="0" step="1" /></label>
            <label class="form-field"><span>原因</span><input v-model="maintenanceForm.reason" class="text-input" /></label>
            <button class="primary-button" type="submit" :disabled="!isConnected"><Wrench :size="16" /><span>提交任务</span></button>
          </form>
          <pre v-if="maintenanceResult" class="json-result">{{ jsonText(maintenanceResult) }}</pre>
          <div v-else class="empty-state">{{ isConnected ? "选择维护动作并提交。" : "连接控制台后管理维护任务。" }}</div>
        </section>

        <footer class="page-footer">
          <span>JianerAI · 记忆控制台</span>
          <span><ShieldCheck :size="14" aria-hidden="true" /> Token 不会写入浏览器存储</span>
        </footer>
      </main>
    </div>

    <dialog ref="tokenDialog" class="modal" aria-labelledby="connection-title" @close="tokenDraft = ''">
      <form class="modal-form" @submit.prevent="connect">
        <div class="modal-heading">
          <div><div class="modal-index">连接</div><h2 id="connection-title">记忆控制台</h2></div>
          <button class="icon-button" type="button" title="关闭" aria-label="关闭" @click="tokenDialog?.close()"><X :size="18" /></button>
        </div>
        <label class="form-field" for="memory-token"><span>本次启动 Token</span><input id="memory-token" v-model="tokenDraft" class="text-input" type="password" autocomplete="off" required /></label>
        <div v-if="tokenError" class="inline-error" role="alert">{{ tokenError }}</div>
        <div v-if="connectionState === 'error' && connectionError" class="inline-error" role="alert">{{ connectionError }}</div>
        <div class="modal-actions"><button class="secondary-button" type="button" @click="tokenDialog?.close()">取消</button><button class="primary-button" type="submit" :disabled="connectionState === 'connecting'"><LoaderCircle v-if="connectionState === 'connecting'" :size="16" class="spin" /><ArrowRight v-else :size="16" /><span>连接</span></button></div>
      </form>
    </dialog>

    <dialog ref="recordDialog" class="modal record-modal" aria-labelledby="record-title">
      <div class="modal-heading">
        <div><div class="modal-index">{{ resourceLabels[currentView] || "记忆" }}</div><h2 id="record-title">记录详情</h2></div>
        <button class="icon-button" type="button" title="关闭" aria-label="关闭" @click="recordDialog?.close()"><X :size="18" /></button>
      </div>
      <pre class="json-result">{{ jsonText(selectedRecord) }}</pre>
    </dialog>
  </div>
</template>
