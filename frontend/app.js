const state = {
  sessionId: "",
  closed: false,
  busy: false,
  token: "",
  me: null,
  agentCatalog: [],
  onboarding: null,
};

const messagesEl = document.getElementById("messages");
const metaEl = document.getElementById("session-meta");
const formEl = document.getElementById("chat-form");
const inputEl = document.getElementById("message-input");
const sendBtnEl = document.getElementById("send-btn");
const newSessionBtnEl = document.getElementById("new-session-btn");
const cpStatusEl = document.getElementById("cp-status");
const signInFormEl = document.getElementById("signin-form");
const emailInputEl = document.getElementById("email-input");
const passwordInputEl = document.getElementById("password-input");
const signInBtnEl = document.getElementById("signin-btn");
const signOutBtnEl = document.getElementById("signout-btn");
const refreshAgentsBtnEl = document.getElementById("refresh-agents-btn");
const onboardBtnEl = document.getElementById("onboard-btn");
const onboardingStatusEl = document.getElementById("onboarding-status");
const agentSelectEl = document.getElementById("agent-select");
const modeSelectEl = document.getElementById("mode-select");

const TOKEN_KEY = "simpleflow_control_plane_token";

function authHeaders() {
  if (state.token === "") {
    return { "Content-Type": "application/json" };
  }
  return {
    "Content-Type": "application/json",
    Authorization: `Bearer ${state.token}`,
  };
}

function addBubble(role, text) {
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}`;
  bubble.textContent = text;
  messagesEl.appendChild(bubble);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function setBusy(nextBusy) {
  state.busy = nextBusy;
  sendBtnEl.disabled = nextBusy || state.closed;
  newSessionBtnEl.disabled = nextBusy;
  signInBtnEl.disabled = nextBusy;
  refreshAgentsBtnEl.disabled = nextBusy;
  onboardBtnEl.disabled = nextBusy;
  signOutBtnEl.disabled = nextBusy || state.token === "";
  inputEl.disabled = nextBusy || state.closed;
}

function setSessionMeta(extra = "") {
  if (state.sessionId === "") {
    metaEl.textContent = "No session yet";
    return;
  }
  const status = state.closed ? "closed" : "active";
  metaEl.textContent = `Session: ${state.sessionId} (${status})${extra ? ` - ${extra}` : ""}`;
}

function setControlPlaneStatus(text) {
  cpStatusEl.textContent = text;
}

function setOnboardingStatus(text) {
  onboardingStatusEl.textContent = text;
}

function selectedAgent() {
  const idx = agentSelectEl.selectedIndex;
  if (idx < 0 || idx >= state.agentCatalog.length) {
    return null;
  }
  return state.agentCatalog[idx];
}

function renderAgentCatalog() {
  const previousValue = agentSelectEl.value;
  agentSelectEl.innerHTML = "";

  if (!Array.isArray(state.agentCatalog) || state.agentCatalog.length === 0) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No agents available";
    agentSelectEl.appendChild(option);
    return;
  }

  state.agentCatalog.forEach((agent, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    const agentId = String(agent.agent_id || "").trim();
    const agentVersion = String(agent.agent_version || "v1").trim();
    const runtimeId = String(agent.runtime_id || "").trim();
    option.textContent = runtimeId
      ? `${agentId} @ ${agentVersion} (${runtimeId})`
      : `${agentId} @ ${agentVersion}`;
    agentSelectEl.appendChild(option);
  });

  if (previousValue !== "" && Number(previousValue) < state.agentCatalog.length) {
    agentSelectEl.value = previousValue;
  }
}

async function checkControlPlaneHealth() {
  try {
    const response = await fetch("/api/control-plane/health");
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Failed to read control-plane health");
    }

    if (payload.configured === true) {
      setControlPlaneStatus(`Connected to ${payload.base_url || "control-plane"}`);
    } else {
      setControlPlaneStatus("Control-plane not configured. Local workflow mode still works.");
    }
  } catch (error) {
    setControlPlaneStatus(`Control-plane status error: ${String(error.message || error)}`);
  }
}

async function loadProfileAndCatalog() {
  if (state.token === "") {
    state.me = null;
    state.agentCatalog = [];
    renderAgentCatalog();
    return;
  }

  try {
    const meResponse = await fetch("/api/control-plane/me", {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    const mePayload = await meResponse.json();
    if (!meResponse.ok) {
      throw new Error(mePayload.detail || "Failed to load profile");
    }
    state.me = mePayload;

    const agentsResponse = await fetch("/api/agents/available", {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    const agentsPayload = await agentsResponse.json();
    if (!agentsResponse.ok) {
      throw new Error(agentsPayload.detail || "Failed to load agents");
    }

    state.agentCatalog = Array.isArray(agentsPayload.agents)
      ? agentsPayload.agents
      : [];
    renderAgentCatalog();

    const userId = String(state.me.user_id || state.me.id || "").trim();
    setControlPlaneStatus(
      `Signed in${userId ? ` as ${userId}` : ""}. ${state.agentCatalog.length} agent(s) loaded.`
    );
  } catch (error) {
    state.me = null;
    state.agentCatalog = [];
    renderAgentCatalog();
    setControlPlaneStatus(`Control-plane load error: ${String(error.message || error)}`);
    addBubble("system", `Control-plane load error: ${String(error.message || error)}`);
  }
}

async function signIn(email, password) {
  setBusy(true);
  try {
    const response = await fetch("/api/control-plane/sign-in", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Sign-in failed");
    }

    const token = String(
      payload.access_token || payload.token || payload.session_token || ""
    ).trim();
    if (token === "") {
      throw new Error("Sign-in succeeded but token was not returned.");
    }

    state.token = token;
    localStorage.setItem(TOKEN_KEY, token);
    passwordInputEl.value = "";
    await loadProfileAndCatalog();
    addBubble("system", "Control-plane sign-in successful.");
  } catch (error) {
    addBubble("system", `Sign-in error: ${String(error.message || error)}`);
  } finally {
    setBusy(false);
  }
}

async function signOut() {
  if (state.token === "") {
    return;
  }
  setBusy(true);
  try {
    await fetch("/api/control-plane/sign-out", {
      method: "DELETE",
      headers: { Authorization: `Bearer ${state.token}` },
    });
  } finally {
    state.token = "";
    state.me = null;
    state.agentCatalog = [];
    localStorage.removeItem(TOKEN_KEY);
    renderAgentCatalog();
    setControlPlaneStatus("Signed out. Local workflow mode still available.");
    setBusy(false);
  }
}

async function onboardSelectedAgent() {
  const agent = selectedAgent();
  if (agent == null) {
    addBubble("system", "No selectable agent for onboarding.");
    return;
  }
  if (state.token === "") {
    addBubble("system", "Sign in first to run onboarding.");
    return;
  }

  setBusy(true);
  try {
    const response = await fetch("/api/onboarding/start", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({
        agent_id: String(agent.agent_id || ""),
        agent_version: String(agent.agent_version || ""),
      }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Onboarding failed");
    }

    state.onboarding = payload;
    const status = String(payload.overall_status || "unknown");
    const registrationId = String(payload.registration_id || "");
    setOnboardingStatus(
      registrationId
        ? `Onboarding ${status}. Registration: ${registrationId}`
        : `Onboarding ${status}.`
    );
    addBubble("system", `Onboarding finished with status: ${status}`);
  } catch (error) {
    addBubble("system", `Onboarding error: ${String(error.message || error)}`);
    setOnboardingStatus(`Onboarding error: ${String(error.message || error)}`);
  } finally {
    setBusy(false);
  }
}

async function createSession() {
  setBusy(true);
  try {
    const response = await fetch("/api/session", { method: "POST" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Failed to create session");
    }

    state.sessionId = String(payload.session_id || "").trim();
    state.closed = false;
    messagesEl.innerHTML = "";
    addBubble(
      "system",
      "New session started. Ask interview questions or paste candidate answers."
    );
    setSessionMeta();
    inputEl.focus();
  } catch (error) {
    addBubble("system", `Session error: ${String(error.message || error)}`);
  } finally {
    setBusy(false);
  }
}

async function sendMessage(text) {
  if (state.sessionId === "") {
    await createSession();
  }

  if (state.closed) {
    addBubble("system", "Interview is closed. Start a new session.");
    return;
  }

  setBusy(true);
  addBubble("user", text);

  try {
    const mode = modeSelectEl.value;
    let response;
    if (mode === "control-plane") {
      if (state.token === "") {
        throw new Error("Sign in before using control-plane mode.");
      }
      const agent = selectedAgent();
      if (agent == null) {
        throw new Error("Select an agent before control-plane invoke.");
      }

      const meUserId = String(
        (state.me && (state.me.user_id || state.me.id)) || ""
      ).trim();
      const invokePayload = {
        agent_id: String(agent.agent_id || ""),
        agent_version: String(agent.agent_version || "v1"),
        mode: "realtime",
        chat_id: state.sessionId,
        user_id: meUserId,
        input: {
          message: text,
          messages: [{ role: "user", content: text }],
        },
      };

      response = await fetch("/api/control-plane/chat/invoke", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify(invokePayload),
      });
    } else {
      response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: state.sessionId, message: text }),
      });
    }

    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "Chat request failed");
    }

    const reply = String(
      payload.reply ||
        payload.output?.reply ||
        payload.output?.message ||
        payload.message ||
        ""
    ).trim();
    addBubble("assistant", reply || "No response from workflow.");

    state.closed = payload.closed === true || payload.status === "terminated";
    if (state.closed) {
      addBubble(
        "system",
        "Interview has reached a terminal state. Create a new session for another candidate."
      );
    }
    setSessionMeta();
  } catch (error) {
    addBubble("system", `Chat error: ${String(error.message || error)}`);
    setSessionMeta("error");
  } finally {
    setBusy(false);
  }
}

formEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = inputEl.value.trim();
  if (text === "" || state.busy) {
    return;
  }
  inputEl.value = "";
  await sendMessage(text);
});

newSessionBtnEl.addEventListener("click", async () => {
  if (state.busy) {
    return;
  }
  await createSession();
});

signInFormEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const email = emailInputEl.value.trim();
  const password = passwordInputEl.value.trim();
  if (email === "" || password === "" || state.busy) {
    return;
  }
  await signIn(email, password);
});

refreshAgentsBtnEl.addEventListener("click", async () => {
  if (state.busy) {
    return;
  }
  await loadProfileAndCatalog();
});

onboardBtnEl.addEventListener("click", async () => {
  if (state.busy) {
    return;
  }
  await onboardSelectedAgent();
});

signOutBtnEl.addEventListener("click", async () => {
  if (state.busy) {
    return;
  }
  await signOut();
});

async function bootstrap() {
  const token = String(localStorage.getItem(TOKEN_KEY) || "").trim();
  if (token !== "") {
    state.token = token;
  }

  await checkControlPlaneHealth();
  if (state.token !== "") {
    await loadProfileAndCatalog();
  } else {
    renderAgentCatalog();
  }
  await createSession();
}

bootstrap();
