import WebSocket from 'ws';
import { config } from '../config.js';
import { GameState } from '../adapters/base.js';

type GameHandler = (game: GameState) => void;

/**
 * Sports WebSocket feed — live game scores and state.
 * No auth needed. Auto-reconnects.
 */
export class SportsFeed {
  private ws: WebSocket | null = null;
  private handlers: GameHandler[] = [];
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  onGame(handler: GameHandler) {
    this.handlers.push(handler);
  }

  start() {
    this.connect();
    console.log('SportsFeed started');
  }

  stop() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close();
  }

  private connect() {
    this.ws = new WebSocket(config.sportsWs);

    this.ws.on('open', () => console.log('SportsFeed connected'));

    this.ws.on('message', (data) => {
      const msg = data.toString();
      if (msg === 'ping') {
        this.ws?.send('pong');
        return;
      }
      try {
        const game = JSON.parse(msg) as GameState;
        if (game.slug) {
          for (const h of this.handlers) h(game);
        }
      } catch {}
    });

    this.ws.on('close', () => {
      console.log('SportsFeed disconnected, reconnecting in 5s');
      this.reconnectTimer = setTimeout(() => this.connect(), 5000);
    });

    this.ws.on('error', (err) => {
      console.warn(`SportsFeed error: ${err.message}`);
    });

    this.ws.on('ping', () => this.ws?.pong());
  }
}
