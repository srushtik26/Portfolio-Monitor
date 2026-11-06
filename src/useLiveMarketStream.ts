import { useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import {
  computeDistanceMetrics,
  getLiveSseStreamUrl,
  getLiveWebSocketUrl,
} from "./api";
import type {
  ApiAlert,
  LiveMarketStreamEvent,
  LiveOutboxAlertEvent,
  LivePriceTickEvent,
  LiveUiAlertNotification,
  PortfolioCompany,
} from "./types";

export type StreamTransport = "sse" | "websocket";
export type StreamConnectionState = "connecting" | "connected" | "reconnecting" | "disconnected";

export interface UseLiveMarketStreamOptions {
  setPortfolioCompanies: Dispatch<SetStateAction<PortfolioCompany[]>>;
  setApiAlerts: Dispatch<SetStateAction<ApiAlert[]>>;
  onOutboxAlert?: (notification: LiveUiAlertNotification) => void;
  preferredTransport?: StreamTransport;
  enabled?: boolean;
}

export interface LiveMarketStreamStatus {
  state: StreamConnectionState;
  transport: StreamTransport;
  lastEventAt: string | null;
  liveNotifications: LiveUiAlertNotification[];
  dismissLiveNotification: (outboxId: string) => void;
  clearLiveNotifications: () => void;
}

export function applyPriceTickToCompany(
  company: PortfolioCompany,
  tick: LivePriceTickEvent,
): PortfolioCompany {
  const nextCompany: PortfolioCompany = {
    ...company,
    base_target: tick.base_target ?? company.base_target,
    bull_target: tick.bull_target ?? company.bull_target,
    base_direction: tick.base_direction ?? company.base_direction,
    bull_direction: tick.bull_direction ?? company.bull_direction,
    latest_quote: tick.quote,
    base_alert_state: tick.base_alert_state ?? company.base_alert_state,
    bull_alert_state: tick.bull_alert_state ?? company.bull_alert_state,
  };

  // Compute instant Distance-from-Target metrics on the client and prefer server-enriched values when provided
  const computed = computeDistanceMetrics(nextCompany, tick.quote.price);
  return {
    ...nextCompany,
    distance_to_base_abs:
      tick.distance_to_base_abs !== undefined ? tick.distance_to_base_abs : computed.distance_to_base_abs,
    distance_to_base_pct:
      tick.distance_to_base_pct !== undefined ? tick.distance_to_base_pct : computed.distance_to_base_pct,
    distance_to_bull_abs:
      tick.distance_to_bull_abs !== undefined ? tick.distance_to_bull_abs : computed.distance_to_bull_abs,
    distance_to_bull_pct:
      tick.distance_to_bull_pct !== undefined ? tick.distance_to_bull_pct : computed.distance_to_bull_pct,
    nearest_target_pct:
      tick.nearest_target_pct !== undefined ? tick.nearest_target_pct : computed.nearest_target_pct,
    alert_status: tick.alert_status ?? computed.alert_status,
  };
}

export function useLiveMarketStream({
  setPortfolioCompanies,
  setApiAlerts,
  onOutboxAlert,
  preferredTransport = "sse",
  enabled = true,
}: UseLiveMarketStreamOptions): LiveMarketStreamStatus {
  const [state, setState] = useState<StreamConnectionState>("connecting");
  const [transport, setTransport] = useState<StreamTransport>(preferredTransport);
  const [lastEventAt, setLastEventAt] = useState<string | null>(null);
  const [liveNotifications, setLiveNotifications] = useState<LiveUiAlertNotification[]>([]);

  const lastQuoteIdRef = useRef<number | undefined>(undefined);
  const seenOutboxIdsRef = useRef<Set<string>>(new Set());
  const onOutboxAlertRef = useRef(onOutboxAlert);
  onOutboxAlertRef.current = onOutboxAlert;

  const dismissLiveNotification = (outboxId: string) => {
    setLiveNotifications((prev) => prev.filter((item) => item.outboxId !== outboxId));
  };

  const clearLiveNotifications = () => {
    setLiveNotifications([]);
  };

  useEffect(() => {
    if (!enabled) {
      setState("disconnected");
      return;
    }

    let cancelled = false;
    let eventSource: EventSource | null = null;
    let webSocket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let retryAttempt = 0;

    const handleParsedEvent = (eventData: LiveMarketStreamEvent) => {
      if (cancelled) return;
      setLastEventAt(new Date().toISOString());

      if (eventData.type === "connected") {
        setState("connected");
        retryAttempt = 0;
        if (typeof eventData.cursor_quote_id === "number" && eventData.cursor_quote_id > 0) {
          lastQuoteIdRef.current = eventData.cursor_quote_id;
        }
        return;
      }

      if (eventData.type === "heartbeat") {
        setState("connected");
        return;
      }

      if (eventData.type === "price_tick") {
        setState("connected");
        if (eventData.quote?.id) {
          lastQuoteIdRef.current = Math.max(lastQuoteIdRef.current ?? 0, eventData.quote.id);
        }
        setPortfolioCompanies((prevCompanies) =>
          prevCompanies.map((company) => {
            if (
              company.exchange.toUpperCase() === eventData.exchange.toUpperCase() &&
              company.symbol.toUpperCase() === eventData.symbol.toUpperCase()
            ) {
              return applyPriceTickToCompany(company, eventData);
            }
            return company;
          }),
        );
        return;
      }

      if (eventData.type === "outbox_alert") {
        setState("connected");
        const outboxEvent: LiveOutboxAlertEvent = eventData;
        if (seenOutboxIdsRef.current.has(outboxEvent.outbox_id)) {
          return;
        }
        seenOutboxIdsRef.current.add(outboxEvent.outbox_id);

        setApiAlerts((prevAlerts) => {
          const existingIdx = prevAlerts.findIndex((a) => a.id === outboxEvent.alert.id);
          if (existingIdx >= 0) {
            const copy = [...prevAlerts];
            copy[existingIdx] = { ...copy[existingIdx], ...outboxEvent.alert };
            return copy;
          }
          return [outboxEvent.alert, ...prevAlerts];
        });

        const uiNotification: LiveUiAlertNotification = {
          outboxId: outboxEvent.outbox_id,
          alert: outboxEvent.alert,
          channel: outboxEvent.channel,
          createdAt: outboxEvent.created_at,
        };

        setLiveNotifications((prev) => [uiNotification, ...prev.slice(0, 4)]);
        onOutboxAlertRef.current?.(uiNotification);

        // Optional browser desktop notification if user has granted permission
        if (typeof window !== "undefined" && "Notification" in window && Notification.permission === "granted") {
          const kindLabel = outboxEvent.alert.target_kind === "base" ? "Base Target" : "Bull Target";
          try {
            new Notification(`Price Target Alert – ${outboxEvent.alert.symbol}`, {
              body: `${outboxEvent.alert.company} (${outboxEvent.alert.exchange}:${outboxEvent.alert.symbol}) reached its ${kindLabel} at ₹${outboxEvent.alert.price.toFixed(2)}.`,
            });
          } catch {
            // Ignore browser notification errors in restricted contexts
          }
        }
      }
    };

    const scheduleReconnect = (nextTransport: StreamTransport) => {
      if (cancelled) return;
      setState("reconnecting");
      const delayMs = Math.min(1000 * Math.pow(1.6, retryAttempt), 12000);
      retryAttempt += 1;
      reconnectTimer = window.setTimeout(() => {
        if (cancelled) return;
        if (nextTransport === "websocket") {
          connectWebSocket();
        } else {
          connectSse();
        }
      }, delayMs);
    };

    const connectWebSocket = () => {
      if (cancelled) return;
      setTransport("websocket");
      setState(retryAttempt > 0 ? "reconnecting" : "connecting");
      const wsUrl = getLiveWebSocketUrl({ sinceQuoteId: lastQuoteIdRef.current });
      try {
        webSocket = new WebSocket(wsUrl);
      } catch {
        scheduleReconnect("sse");
        return;
      }

      webSocket.onopen = () => {
        if (!cancelled) {
          setState("connected");
          retryAttempt = 0;
        }
      };

      webSocket.onmessage = (evt: MessageEvent) => {
        try {
          const parsed = JSON.parse(String(evt.data)) as LiveMarketStreamEvent;
          handleParsedEvent(parsed);
        } catch {
          // Ignore malformed frame
        }
      };

      webSocket.onerror = () => {
        webSocket?.close();
      };

      webSocket.onclose = () => {
        webSocket = null;
        if (!cancelled) {
          // Alternate back to SSE if WebSocket drops repeatedly
          scheduleReconnect(retryAttempt >= 2 ? "sse" : "websocket");
        }
      };
    };

    const connectSse = () => {
      if (cancelled) return;
      if (typeof EventSource === "undefined") {
        connectWebSocket();
        return;
      }
      setTransport("sse");
      setState(retryAttempt > 0 ? "reconnecting" : "connecting");
      const sseUrl = getLiveSseStreamUrl({ sinceQuoteId: lastQuoteIdRef.current });
      try {
        eventSource = new EventSource(sseUrl);
      } catch {
        connectWebSocket();
        return;
      }

      const listener = (evt: Event) => {
        const msgEvt = evt as MessageEvent;
        if (!msgEvt.data) return;
        try {
          const parsed = JSON.parse(String(msgEvt.data)) as LiveMarketStreamEvent;
          handleParsedEvent(parsed);
        } catch {
          // Ignore malformed SSE payload
        }
      };

      eventSource.addEventListener("connected", listener);
      eventSource.addEventListener("heartbeat", listener);
      eventSource.addEventListener("price_tick", listener);
      eventSource.addEventListener("outbox_alert", listener);
      eventSource.onmessage = listener;

      eventSource.onerror = () => {
        eventSource?.close();
        eventSource = null;
        if (!cancelled) {
          // Fall back to WebSocket if SSE fails more than once
          scheduleReconnect(retryAttempt >= 1 ? "websocket" : "sse");
        }
      };
    };

    if (preferredTransport === "websocket") {
      connectWebSocket();
    } else {
      connectSse();
    }

    return () => {
      cancelled = true;
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
      }
      if (eventSource !== null) {
        eventSource.close();
      }
      if (webSocket !== null) {
        webSocket.close();
      }
    };
  }, [enabled, preferredTransport, setApiAlerts, setPortfolioCompanies]);

  return {
    state,
    transport,
    lastEventAt,
    liveNotifications,
    dismissLiveNotification,
    clearLiveNotifications,
  };
}

