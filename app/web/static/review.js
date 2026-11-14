"use strict";

(() => {
  const prefix = document.querySelector('meta[name="review-api-prefix"]').content;
  const api = `${prefix}/review`;
  const storageKey = `local-review:${api}:preview`;
  const $ = (id) => document.getElementById(id);
  const controls = {
    compose: $("compose-form"), email: $("sender-email"), message: $("message"),
    create: $("create-preview"), fresh: $("new-message"), count: $("character-count"),
    content: $("preview-content"), empty: $("empty-preview"), state: $("state-label"),
    originalEmail: $("original-email"), originalMessage: $("original-message"),
    time: $("preview-time"), expiry: $("expiry-label"), approval: $("approval-form"),
    department: $("department"), confirmed: $("confirmed"), approve: $("approve-send"),
    reject: $("reject-preview"), suggest: $("request-suggestion"),
    suggestionStatus: $("suggestion-status"), suggestionDetail: $("suggestion-detail"),
    suggestedRecipient: $("suggested-recipient"), reason: $("suggestion-reason"),
    quotes: $("issue-quotes"), result: $("delivery-result"), resultTitle: $("delivery-title"),
    resultDescription: $("delivery-description"), identifiers: $("delivery-identifiers"),
    messageId: $("delivery-message-id"), attempts: $("delivery-attempts"),
    retry: $("retry-send"), refresh: $("refresh-status"), notice: $("notice"),
    connection: $("connection-status"),
  };
  const states = {
    pending_review: "Czeka na akceptację", approved: "Zatwierdzone", sending: "Wysyłanie",
    sent: "Wysłano", failed_before_submit: "Nie przekazano", delivery_unknown: "Wynik niepewny",
    rejected: "Odrzucono", expired: "Podgląd wygasł",
  };
  const terminalStates = new Set(["sent", "delivery_unknown", "rejected", "expired"]);
  let csrf = "";
  let departments = [];
  let current = null;
  let sessionReady = false;
  let actionBusy = false;
  let modelBusy = false;
  let generation = 0;
  let pollTimer = null;

  class ApiError extends Error {
    constructor(status, detail) { super(detail); this.status = status; }
  }

  function remember(id) {
    try {
      if (id) sessionStorage.setItem(storageKey, id);
      else sessionStorage.removeItem(storageKey);
    } catch (_) { /* The workflow still works when browser storage is unavailable. */ }
  }

  function remembered() {
    try { return sessionStorage.getItem(storageKey); }
    catch (_) { return null; }
  }

  function showNotice(message, kind = "error") {
    controls.notice.textContent = message;
    controls.notice.dataset.kind = kind;
    controls.notice.hidden = !message;
  }

  function requestError(error) {
    if (!(error instanceof ApiError)) return "Nie udało się połączyć. Odśwież stan przed ponowieniem wysyłki.";
    if (error.status === 403) return "Sesja wygasła lub żądanie zostało odrzucone. Odśwież stronę.";
    if (error.status === 404) return "Podgląd nie jest dostępny w tej sesji. Utwórz nowy.";
    if (error.status === 409) return "Stan podglądu się zmienił. Odśwież go i sprawdź ponownie.";
    if (error.status === 422) return "Sprawdź adres e-mail, treść i wybrany dział.";
    if (error.status === 429) return "Aplikacja jest zajęta. Spróbuj ponownie za chwilę.";
    return "Nie udało się zakończyć operacji. Odśwież stan przed ponowieniem.";
  }

  async function request(path, method = "GET", body) {
    const headers = { Accept: "application/json" };
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      headers["X-CSRF-Token"] = csrf;
    }
    const response = await fetch(`${api}${path}`, {
      method, headers, credentials: "same-origin", cache: "no-store",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data;
    try { data = await response.json(); }
    catch (_) { throw new ApiError(response.status, "Invalid response"); }
    if (!response.ok) throw new ApiError(response.status, typeof data.detail === "string" ? data.detail : "Request rejected");
    return data;
  }

  function dateLabel(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString("pl-PL", { hour: "2-digit", minute: "2-digit" });
  }

  function departmentLabel(recipient) {
    const item = departments.find((entry) => entry.recipient === recipient);
    return item ? `${item.label} (${item.recipient})` : recipient;
  }

  function countCharacters() {
    controls.count.textContent = `${controls.message.value.length} / ${controls.message.maxLength}`;
  }

  function updateActions() {
    const pending = current && current.state === "pending_review";
    const knownChoice = departments.some((entry) => entry.recipient === controls.department.value);
    controls.create.disabled = !sessionReady || actionBusy || Boolean(current && current.state !== "pending_review");
    controls.suggest.disabled = !pending || modelBusy || actionBusy;
    controls.department.disabled = !pending || actionBusy;
    controls.confirmed.disabled = !pending || actionBusy;
    controls.approve.disabled = !pending || actionBusy || !knownChoice || !controls.confirmed.checked;
    controls.reject.disabled = !pending || actionBusy;
    controls.refresh.disabled = !current || actionBusy;
    controls.retry.disabled = !current || current.state !== "failed_before_submit" || current.retry_allowed !== true || actionBusy;
    const locked = current && current.state !== "pending_review";
    controls.email.disabled = Boolean(!sessionReady || locked || actionBusy);
    controls.message.disabled = Boolean(!sessionReady || locked || actionBusy);
    controls.fresh.hidden = !current || current.state === "pending_review" || current.state === "sending";
    controls.fresh.disabled = actionBusy;
  }

  function renderSuggestion() {
    const suggestion = current?.suggestion || { status: "not_requested" };
    controls.suggestionDetail.hidden = true;
    controls.quotes.replaceChildren();
    controls.suggest.textContent = modelBusy ? "Pobieranie propozycji..." : "Poproś o propozycję";
    if (modelBusy || suggestion.status === "pending") {
      controls.suggestionStatus.textContent = "Trwa pobieranie propozycji. Możesz wybrać dział i zatwierdzić wiadomość ręcznie.";
      return;
    }
    if (suggestion.status === "ready" && suggestion.recipient) {
      controls.suggestionStatus.textContent = suggestion.used_fallback
        ? "Propozycja awaryjna. Sprawdź dział przed zatwierdzeniem."
        : "To podpowiedź do sprawdzenia. Wybór działu należy do Ciebie.";
      controls.suggestedRecipient.textContent = departmentLabel(suggestion.recipient);
      controls.reason.textContent = suggestion.reason || "Model nie podał uzasadnienia.";
      for (const quote of suggestion.issue_quotes || []) {
        const item = document.createElement("li");
        item.textContent = quote;
        controls.quotes.append(item);
      }
      $("quote-details").hidden = !controls.quotes.childElementCount;
      controls.suggestionDetail.hidden = false;
      return;
    }
    controls.suggestionStatus.textContent = suggestion.status === "error"
      ? "Propozycja jest niedostępna. Możesz wybrać dział i zatwierdzić wiadomość ręcznie."
      : "Nie pobrano propozycji. Możesz wybrać dział ręcznie.";
  }

  function renderDelivery() {
    const state = current.state;
    const descriptions = {
      approved: ["Wiadomość zatwierdzona", "Zgoda jest zapisana. Odśwież stan, aby sprawdzić wysyłkę."],
      sending: ["Trwa wysyłka", "Nie ponawiaj żądania. Możesz odświeżyć stan tej operacji."],
      sent: ["Wiadomość wysłana do Mailpit", `Zatwierdzony dział: ${departmentLabel(current.recipient)}.`],
      failed_before_submit: ["Wiadomość nie została przekazana", current.retry_allowed === true
        ? "Błąd wystąpił przed przekazaniem wiadomości. Możesz ponowić tę samą zatwierdzoną wysyłkę."
        : "Błąd wystąpił przed przekazaniem wiadomości. Ponowienie tej operacji jest niedostępne."],
      delivery_unknown: ["Wynik wysyłki jest niepewny", "Mailpit mógł przyjąć wiadomość. Sprawdź ją po Message-ID. Ta operacja nie zostanie automatycznie ponowiona."],
      rejected: ["Podgląd odrzucony", "Nie wysłano wiadomości. Możesz utworzyć nowe zgłoszenie."],
      expired: ["Podgląd wygasł", "Nie można już zatwierdzić tego podglądu. Utwórz nowy."],
    };
    const description = descriptions[state];
    controls.result.hidden = !description;
    controls.result.dataset.state = state;
    controls.approval.hidden = state !== "pending_review";
    controls.retry.hidden = state !== "failed_before_submit" || current.retry_allowed !== true;
    if (!description) return;
    controls.resultTitle.textContent = description[0];
    controls.resultDescription.textContent = description[1];
    controls.identifiers.hidden = !current.message_id;
    controls.messageId.textContent = current.message_id || "";
    controls.attempts.textContent = String(Array.isArray(current.attempts) ? current.attempts.length : 0);
  }

  function renderPreview(next, focus = false) {
    if (!next || typeof next.id !== "string") throw new Error("Missing preview record");
    if (current && current.id === next.id) {
      if (Number(next.version) < Number(current.version)) return;
      if (terminalStates.has(current.state) && next.state !== current.state) return;
      if (current.state === "sending" && next.state === "pending_review") return;
      if (next.version !== current.version) controls.confirmed.checked = false;
    }
    current = next;
    remember(current.id);
    controls.empty.hidden = true;
    controls.content.hidden = false;
    controls.state.textContent = states[current.state] || "Sprawdź stan";
    controls.state.dataset.state = current.state;
    controls.originalEmail.textContent = current.email;
    controls.originalMessage.textContent = current.message;
    controls.time.textContent = dateLabel(current.created_at);
    controls.expiry.textContent = current.state === "pending_review"
      ? `Podgląd ważny do ${dateLabel(current.expires_at)}` : "Zapisany stan operacji";
    if (current.recipient && current.state !== "pending_review") controls.department.value = current.recipient;
    renderSuggestion();
    renderDelivery();
    updateActions();
    clearTimeout(pollTimer);
    if (current.state === "sending") pollTimer = setTimeout(() => refreshStatus(false), 2000);
    if (focus) $("review-title").focus();
  }

  function invalidatePreview() {
    countCharacters();
    if (!current || current.state !== "pending_review") return;
    generation += 1;
    current = null;
    modelBusy = false;
    remember(null);
    clearTimeout(pollTimer);
    controls.content.hidden = true;
    controls.empty.hidden = false;
    controls.confirmed.checked = false;
    controls.department.value = "";
    controls.state.textContent = "Utwórz nowy podgląd";
    controls.state.removeAttribute("data-state");
    showNotice("Oryginał został zmieniony. Utwórz nowy podgląd przed wysyłką.", "info");
    updateActions();
  }

  async function refreshStatus(showError = true) {
    if (!current) return;
    const id = current.id;
    const started = generation;
    try {
      const next = await request(`/previews/${encodeURIComponent(id)}`);
      if (current?.id === id && generation === started) renderPreview(next);
    } catch (error) {
      if (showError) showNotice(requestError(error));
    }
  }

  controls.compose.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!sessionReady || actionBusy || !controls.compose.reportValidity()) return;
    actionBusy = true;
    generation += 1;
    const started = generation;
    showNotice("");
    updateActions();
    try {
      const next = await request("/previews", "POST", { email: controls.email.value, message: controls.message.value });
      if (generation !== started) return;
      controls.confirmed.checked = false;
      controls.department.value = "";
      modelBusy = false;
      renderPreview(next, true);
    } catch (error) { showNotice(requestError(error)); }
    finally { actionBusy = false; updateActions(); }
  });

  controls.suggest.addEventListener("click", async () => {
    if (!current || current.state !== "pending_review" || modelBusy) return;
    const id = current.id;
    const started = generation;
    const version = current.version;
    modelBusy = true;
    showNotice("");
    renderSuggestion();
    updateActions();
    try {
      const next = await request(`/previews/${encodeURIComponent(id)}/suggest`, "POST", { version });
      if (generation === started && current?.id === id && current.state === "pending_review") renderPreview(next);
    } catch (error) {
      if (generation === started && current?.id === id && current.state === "pending_review") {
        showNotice(error instanceof ApiError && error.status === 409
          ? "Podgląd zmienił stan. Odśwież go przed dalszą pracą."
          : "Nie udało się pobrać propozycji. Nadal możesz wybrać dział ręcznie.", "info");
      }
    } finally {
      if (generation === started && current?.id === id) {
        modelBusy = false;
        renderSuggestion();
        updateActions();
      }
    }
  });

  controls.approval.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!current || current.state !== "pending_review" || actionBusy || !controls.approval.reportValidity()) return;
    if (!departments.some((item) => item.recipient === controls.department.value) || !controls.confirmed.checked) return;
    const id = current.id;
    const payload = { version: current.version, payload_digest: current.payload_digest, recipient: controls.department.value, confirmed: true };
    actionBusy = true;
    generation += 1;
    modelBusy = false;
    showNotice("");
    updateActions();
    try { renderPreview(await request(`/previews/${encodeURIComponent(id)}/approve`, "POST", payload), true); }
    catch (error) { showNotice(requestError(error)); await refreshStatus(false); }
    finally { actionBusy = false; updateActions(); }
  });

  controls.retry.addEventListener("click", async () => {
    if (!current || current.state !== "failed_before_submit" || current.retry_allowed !== true || actionBusy) return;
    const id = current.id;
    const payload = { version: current.version, payload_digest: current.payload_digest };
    actionBusy = true;
    generation += 1;
    showNotice("");
    updateActions();
    try { renderPreview(await request(`/previews/${encodeURIComponent(id)}/retry`, "POST", payload), true); }
    catch (error) { showNotice(requestError(error)); await refreshStatus(false); }
    finally { actionBusy = false; updateActions(); }
  });

  controls.reject.addEventListener("click", async () => {
    if (!current || current.state !== "pending_review" || actionBusy) return;
    const id = current.id;
    const version = current.version;
    actionBusy = true;
    generation += 1;
    modelBusy = false;
    updateActions();
    try { renderPreview(await request(`/previews/${encodeURIComponent(id)}/reject`, "POST", { version }), true); }
    catch (error) { showNotice(requestError(error)); await refreshStatus(false); }
    finally { actionBusy = false; updateActions(); }
  });

  controls.fresh.addEventListener("click", () => {
    if (actionBusy || current?.state === "sending") return;
    generation += 1;
    current = null;
    modelBusy = false;
    clearTimeout(pollTimer);
    remember(null);
    controls.compose.reset();
    controls.department.value = "";
    controls.confirmed.checked = false;
    controls.content.hidden = true;
    controls.empty.hidden = false;
    controls.state.textContent = "Brak podglądu";
    controls.state.removeAttribute("data-state");
    showNotice("");
    countCharacters();
    updateActions();
    controls.email.focus();
  });
  controls.refresh.addEventListener("click", () => refreshStatus());
  controls.email.addEventListener("input", invalidatePreview);
  controls.message.addEventListener("input", invalidatePreview);
  controls.department.addEventListener("change", () => { controls.confirmed.checked = false; updateActions(); });
  controls.confirmed.addEventListener("change", updateActions);

  async function bootstrap() {
    try {
      const session = await request("/session");
      if (typeof session.csrf_token !== "string" || !Array.isArray(session.departments)) throw new Error("Invalid session");
      csrf = session.csrf_token;
      departments = session.departments.filter((item) => typeof item.recipient === "string" && typeof item.label === "string");
      for (const item of departments) {
        const option = document.createElement("option");
        option.value = item.recipient;
        option.textContent = departmentLabel(item.recipient);
        controls.department.append(option);
      }
      if (!departments.length) throw new Error("No departments");
      updateActions();
      const id = remembered();
      if (id) {
        try {
          const previous = await request(`/previews/${encodeURIComponent(id)}`);
          controls.email.value = previous.email;
          controls.message.value = previous.message;
          countCharacters();
          renderPreview(previous);
        } catch (_) {
          remember(null);
          showNotice("Poprzedni podgląd jest niedostępny w tej sesji. Możesz utworzyć nowy.", "info");
        }
      }
      sessionReady = true;
      controls.connection.textContent = "Lokalna sesja gotowa. Żadna wiadomość nie zostanie wysłana bez zatwierdzenia.";
      updateActions();
    } catch (_) {
      controls.connection.textContent = "Nie udało się przygotować sesji.";
      showNotice("Nie można teraz utworzyć podglądu. Odśwież stronę, aby ponowić połączenie.");
    }
  }

  countCharacters();
  bootstrap();
})();
