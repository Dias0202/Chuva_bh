"use strict";

// Configuração pública gerada no deploy (região e ID do cliente do Cognito, sem segredos).
const CONFIG = window.CHUVA_CONFIG || {};
const COGNITO_URL = `https://cognito-idp.${CONFIG.region}.amazonaws.com/`;
const TOKEN_KEY = "chuva-bh-session";
const STALE_HOURS = 3;
const TIME = new Intl.DateTimeFormat("pt-BR", {
  timeZone: "America/Sao_Paulo",
  day: "2-digit",
  month: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, text) {
  // Sempre textContent: nada que vem da rede é inserido como HTML.
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

function utc(value) {
  // A API usa "AAAA-MM-DDTHH:MM" em UTC, sem segundos nem fuso.
  if (value.endsWith("Z")) return new Date(value);
  return new Date(value.length === 16 ? `${value}:00Z` : `${value}Z`);
}

function renderPoint(point) {
  const card = el("article", { class: "card" });
  card.append(el("h3", {}, point.name), el("div", { class: "code" }, point.id));
  const forecast = point.forecast;
  if (!forecast) {
    card.classList.add("unavailable");
    card.append(el("p", { class: "meta" }, "Sem previsão ao vivo: depende da chave da API do INMET."));
    return card;
  }
  for (const item of forecast.forecasts) {
    const pct = Math.round(item.probability * 1000) / 10;
    const row = el("div", { class: "horizon" });
    row.append(el("span", {}, TIME.format(utc(item.valid_at))));
    const track = el("div", { class: "track", role: "img", "aria-label": `${pct}% de chance de chuva` });
    const fill = el("div", { class: "fill" });
    fill.style.width = `${Math.min(100, Math.max(pct, 1))}%`;
    track.append(fill);
    row.append(track, el("span", { class: "pct" }, `${pct.toLocaleString("pt-BR")}%`));
    card.append(row);
  }
  const issued = utc(forecast.issued_at);
  const ageHours = (Date.now() - issued.getTime()) / 3_600_000;
  const meta = el("p", { class: "meta" }, `Emitida em ${TIME.format(issued)} · modelo v${forecast.model_version}`);
  if (ageHours > STALE_HOURS) {
    meta.append(el("span", { class: "stale" }, " · desatualizada"));
  }
  card.append(meta);
  return card;
}

async function loadForecasts() {
  const container = $("points");
  try {
    const response = await fetch("data/forecasts.json", { cache: "no-cache" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    container.replaceChildren(...data.points.map(renderPoint));
    $("generated").textContent = `Atualizado em ${TIME.format(utc(data.generated_at))}`;
    $("disclaimer").textContent = data.disclaimer;
  } catch (error) {
    container.replaceChildren(el("p", { class: "error" }, "Não foi possível carregar as previsões."));
  }
}

// --- Login no Cognito (fluxo USER_PASSWORD_AUTH, sempre por HTTPS) ---

async function cognito(target, body) {
  const response = await fetch(COGNITO_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}`,
    },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.message || "Falha na autenticação");
  }
  return data;
}

function saveSession(result) {
  const session = { token: result.IdToken, expires: Date.now() + result.ExpiresIn * 1000 };
  sessionStorage.setItem(TOKEN_KEY, JSON.stringify(session));
}

function currentToken() {
  try {
    const session = JSON.parse(sessionStorage.getItem(TOKEN_KEY) || "null");
    if (session && session.expires > Date.now() + 30_000) return session.token;
  } catch (error) {
    // Sessão corrompida: trata como deslogado.
  }
  sessionStorage.removeItem(TOKEN_KEY);
  return null;
}

let pendingChallenge = null;

function showChat(loggedIn) {
  $("login").hidden = loggedIn || pendingChallenge !== null;
  $("new-password").hidden = pendingChallenge === null;
  $("chat").hidden = !loggedIn;
  $("logout").hidden = !loggedIn;
}

async function onLogin(event) {
  event.preventDefault();
  $("login-error").textContent = "";
  const username = $("email").value.trim();
  try {
    const data = await cognito("InitiateAuth", {
      AuthFlow: "USER_PASSWORD_AUTH",
      ClientId: CONFIG.clientId,
      AuthParameters: { USERNAME: username, PASSWORD: $("password").value },
    });
    $("password").value = "";
    if (data.ChallengeName === "NEW_PASSWORD_REQUIRED") {
      pendingChallenge = { username, session: data.Session };
      showChat(false);
      return;
    }
    saveSession(data.AuthenticationResult);
    showChat(true);
  } catch (error) {
    $("login-error").textContent = "E-mail ou senha incorretos.";
  }
}

async function onNewPassword(event) {
  event.preventDefault();
  $("new-password-error").textContent = "";
  try {
    const data = await cognito("RespondToAuthChallenge", {
      ChallengeName: "NEW_PASSWORD_REQUIRED",
      ClientId: CONFIG.clientId,
      Session: pendingChallenge.session,
      ChallengeResponses: {
        USERNAME: pendingChallenge.username,
        NEW_PASSWORD: $("new-password-value").value,
      },
    });
    $("new-password-value").value = "";
    pendingChallenge = null;
    saveSession(data.AuthenticationResult);
    showChat(true);
  } catch (error) {
    $("new-password-error").textContent = error.message;
  }
}

// --- Chat ---

function addMessage(role, text, trace) {
  const item = el("li", { class: role }, text);
  if (trace) item.append(el("div", { class: "trace" }, trace));
  $("messages").append(item);
  item.scrollIntoView({ behavior: "smooth", block: "end" });
}

async function onAsk(event) {
  event.preventDefault();
  const question = $("question").value.trim();
  const token = currentToken();
  if (!question) return;
  if (!token) {
    showChat(false);
    return;
  }
  addMessage("user", question);
  $("question").value = "";
  $("send").disabled = true;
  try {
    const response = await fetch("api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-chuva-token": token },
      body: JSON.stringify({ question }),
    });
    const data = await response.json().catch(() => ({}));
    if (response.status === 401) {
      sessionStorage.removeItem(TOKEN_KEY);
      showChat(false);
      return;
    }
    if (!response.ok) {
      addMessage("agent", data.error || data.message || "Não foi possível responder agora.");
      return;
    }
    const parts = [];
    if (data.tools && data.tools.length) parts.push(`Ferramentas: ${data.tools.join(", ")}`);
    if (data.sources && data.sources.length) parts.push(`Fontes: ${data.sources.join(", ")}`);
    addMessage("agent", data.answer, parts.join(" · "));
  } catch (error) {
    addMessage("agent", "Falha de conexão com o agente.");
  } finally {
    $("send").disabled = false;
  }
}

$("login").addEventListener("submit", onLogin);
$("new-password").addEventListener("submit", onNewPassword);
$("ask").addEventListener("submit", onAsk);
$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("ask").requestSubmit();
  }
});
$("logout").addEventListener("click", () => {
  sessionStorage.removeItem(TOKEN_KEY);
  showChat(false);
});

showChat(currentToken() !== null);
loadForecasts();
setInterval(loadForecasts, 5 * 60 * 1000);
