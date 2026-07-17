/*---------------------------------------------------------------------------------------------
 *  Copyright (c) PulseCode AI. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { $, append } from '../../../../base/browser/dom.js';
import { localize } from '../../../../nls.js';
import { IContextKeyService } from '../../../../platform/contextkey/common/contextkey.js';
import { IContextMenuService } from '../../../../platform/contextview/browser/contextView.js';
import { IHoverService } from '../../../../platform/hover/browser/hover.js';
import { IInstantiationService } from '../../../../platform/instantiation/common/instantiation.js';
import { IKeybindingService } from '../../../../platform/keybinding/common/keybinding.js';
import { IOpenerService } from '../../../../platform/opener/common/opener.js';
import { IThemeService } from '../../../../platform/theme/common/themeService.js';
import { IViewPaneOptions, ViewPane } from '../../../browser/parts/views/viewPane.js';
import { IViewDescriptorService } from '../../../common/views.js';
import { IConfigurationService } from '../../../../platform/configuration/common/configuration.js';

export const PulseAgentViewId = 'workbench.view.pulseAgent';

/**
 * PulseAgentView — a native ViewPane rendered inside the Secondary (Auxiliary) Side Bar.
 *
 * Layout:
 * ┌──────────────────────────┐
 * │  ♥ PulseCode AI Agent    │  ← header with heartbeat indicator
 * ├──────────────────────────┤
 * │                          │
 * │  Thinking... 9s          │  ← scrollable log area
 * │  ▸ Reading file.ts       │
 * │  ▸ Analyzing imports     │
 * │  ...                     │
 * │                          │
 * ├──────────────────────────┤
 * │  [Ask Pulse anything...] │  ← fixed footer: chat input box
 * └──────────────────────────┘
 */
export class PulseAgentView extends ViewPane {

	private bodyElement: HTMLElement | undefined;
	private headerElement: HTMLElement | undefined;
	private heartbeatIndicator: HTMLElement | undefined;
	private logArea: HTMLElement | undefined;
	private footerElement: HTMLElement | undefined;
	private inputElement: HTMLInputElement | undefined;
	private statusDot: HTMLElement | undefined;
	private statusText: HTMLElement | undefined;
	private ws: WebSocket | null = null;
	private reconnectDelay: number = 1000;
	private disposed: boolean = false;

	private elapsedTimer: ReturnType<typeof setInterval> | undefined;
	private elapsedSeconds: number = 0;

	constructor(
		options: IViewPaneOptions,
		@IKeybindingService keybindingService: IKeybindingService,
		@IContextMenuService contextMenuService: IContextMenuService,
		@IConfigurationService configurationService: IConfigurationService,
		@IContextKeyService contextKeyService: IContextKeyService,
		@IViewDescriptorService viewDescriptorService: IViewDescriptorService,
		@IInstantiationService instantiationService: IInstantiationService,
		@IOpenerService openerService: IOpenerService,
		@IThemeService themeService: IThemeService,
		@IHoverService hoverService: IHoverService,
	) {
		super(options, keybindingService, contextMenuService, configurationService, contextKeyService, viewDescriptorService, instantiationService, openerService, themeService, hoverService);
	}

	// ─── View lifecycle ────────────────────────────────────────────────

	protected override renderBody(container: HTMLElement): void {
		super.renderBody(container);

		this.bodyElement = append(container, $('.pulse-agent-body'));

		// ── Header: heartbeat bar ──
		this.headerElement = append(this.bodyElement, $('.pulse-agent-header'));

		// Red top border line — the "heartbeat" visual
		this.heartbeatIndicator = append(this.headerElement, $('.pulse-agent-heartbeat'));
		this.heartbeatIndicator.style.background = 'linear-gradient(90deg, #e74c3c, #ff6b6b, #e74c3c)';
		this.heartbeatIndicator.style.height = '2px';
		this.heartbeatIndicator.style.width = '100%';
		this.heartbeatIndicator.style.borderRadius = '1px';

		// Header label
		const labelContainer = append(this.headerElement, $('.pulse-agent-header-label'));
		labelContainer.style.display = 'flex';
		labelContainer.style.alignItems = 'center';
		labelContainer.style.gap = '6px';
		labelContainer.style.padding = '8px 12px 6px';

		const pulseIcon = append(labelContainer, $('span'));
		pulseIcon.textContent = '♥';
		pulseIcon.style.color = '#e74c3c';
		pulseIcon.style.fontSize = '14px';
		pulseIcon.style.fontWeight = 'bold';

		const titleText = append(labelContainer, $('span'));
		titleText.textContent = localize('pulseAgent.title', 'PulseCode AI Agent');
		titleText.style.color = '#cccccc';
		titleText.style.fontSize = '12px';
		titleText.style.fontWeight = '600';
		titleText.style.letterSpacing = '0.5px';
		titleText.style.textTransform = 'uppercase';

		// Status indicator
		this.statusDot = append(labelContainer, $('span'));
		const statusDot = this.statusDot;
		statusDot.style.width = '6px';
		statusDot.style.height = '6px';
		statusDot.style.borderRadius = '50%';
		statusDot.style.background = '#4caf50';
		statusDot.style.marginLeft = 'auto';

		this.statusText = append(labelContainer, $('span'));
		this.statusText.textContent = localize('pulseAgent.status.disconnected', 'Disconnected');
		this.statusText.style.color = '#e74c3c';
		this.statusText.style.fontSize = '11px';

		// ── Thinking / Log Area (scrollable) ──
		const logContainer = append(this.bodyElement, $('.pulse-agent-log-container'));
		logContainer.style.flex = '1';
		logContainer.style.overflow = 'hidden';
		logContainer.style.position = 'relative';

		this.logArea = append(logContainer, $('.pulse-agent-log'));
		this.logArea.style.padding = '8px 12px';
		this.logArea.style.fontSize = '12px';
		this.logArea.style.fontFamily = 'var(--vscode-editor-font-family)';
		this.logArea.style.lineHeight = '1.6';
		this.logArea.style.overflowY = 'auto';
		this.logArea.style.height = '100%';
		this.logArea.style.color = '#cccccc';

		// Placeholder thinking text
		this._addLogEntry('Thinking...', '#9e9e9e');

		// ── Footer: Chat Input ──
		this.footerElement = append(this.bodyElement, $('.pulse-agent-footer'));
		this.footerElement.style.borderTop = '1px solid #333333';
		this.footerElement.style.padding = '8px';

		this.inputElement = document.createElement('input') as HTMLInputElement;
		this.inputElement.type = 'text';
		this.inputElement.placeholder = localize('pulseAgent.input.placeholder', 'Ask Pulse anything...');
		this.inputElement.style.width = '100%';
		this.inputElement.style.padding = '8px 12px';
		this.inputElement.style.background = '#2d2d2d';
		this.inputElement.style.border = '1px solid #3c3c3c';
		this.inputElement.style.borderRadius = '6px';
		this.inputElement.style.color = '#cccccc';
		this.inputElement.style.fontSize = '13px';
		this.inputElement.style.outline = 'none';
		this.inputElement.style.boxSizing = 'border-box';
		this.inputElement.style.fontFamily = 'var(--vscode-editor-font-family)';

		// Focus glow
		this.inputElement.addEventListener('focus', () => {
			if (this.inputElement) {
				this.inputElement.style.borderColor = '#7c4dff';
				this.inputElement.style.boxShadow = '0 0 0 1px rgba(124, 77, 255, 0.3)';
			}
		});
		this.inputElement.addEventListener('blur', () => {
			if (this.inputElement) {
				this.inputElement.style.borderColor = '#3c3c3c';
				this.inputElement.style.boxShadow = 'none';
			}
		});

		// Submit on Enter
		this.inputElement.addEventListener('keydown', (e: KeyboardEvent) => {
			if (e.key === 'Enter' && this.inputElement?.value.trim()) {
				this._onSubmit(this.inputElement.value.trim());
				this.inputElement.value = '';
			}
		});

		this.footerElement.appendChild(this.inputElement);

		// ── Styles ──
		this.bodyElement.style.display = 'flex';
		this.bodyElement.style.flexDirection = 'column';
		this.bodyElement.style.height = '100%';
		this.bodyElement.style.background = '#1e1e1e';

		// Connect to Python backend
		this.connectToAgentBackend();
	}

	protected override layoutBody(height: number, width: number): void {
		super.layoutBody(height, width);
	}

	// ─── Public API ────────────────────────────────────────────────────

	/**
	 * Add a log entry to the thinking area.
	 * @param text  The log message
	 * @param color Optional text color
	 */
	addLogEntry(text: string, color?: string): void {
		this._addLogEntry(text, color);
	}

	/**
	 * Clear all log entries.
	 */
	clearLogs(): void {
		if (this.logArea) {
			this.logArea.innerHTML = '';
		}
	}

	/**
	 * Start a thinking timer (shows "Thinking... Xs").
	 */
	startThinking(): void {
		this.elapsedSeconds = 0;
		this.clearLogs();
		this._addLogEntry(`Thinking... ${this.elapsedSeconds}s`, '#9e9e9e');

		this.elapsedTimer = setInterval(() => {
			this.elapsedSeconds++;
			if (this.logArea) {
				const firstChild = this.logArea.firstElementChild;
				if (firstChild) {
					firstChild.textContent = `Thinking... ${this.elapsedSeconds}s`;
				}
			}
		}, 1000);
	}

	/**
	 * Stop the thinking timer.
	 */
	stopThinking(): void {
		if (this.elapsedTimer) {
			clearInterval(this.elapsedTimer);
			this.elapsedTimer = undefined;
		}
	}

	// ─── Private ───────────────────────────────────────────────────────

	private _addLogEntry(text: string, color?: string): void {
		if (!this.logArea) {
			return;
		}
		const entry = append(this.logArea, $('div'));
		entry.textContent = text;
		entry.style.padding = '2px 0';
		entry.style.whiteSpace = 'pre-wrap';
		entry.style.wordBreak = 'break-word';
		if (color) {
			entry.style.color = color;
		}
		// Auto-scroll to bottom
		this.logArea.scrollTop = this.logArea.scrollHeight;
	}

	private _onSubmit(text: string): void {
		if (!this.inputElement) {
			return;
		}

		// Show the user's message in the log
		this._addLogEntry(`> ${text}`, '#80cbc4');

		// Send via WebSocket if open
		if (this.ws && this.ws.readyState === WebSocket.OPEN) {
			this.ws.send(text);
			this.inputElement.value = '';
		} else {
			this._addLogEntry('[disconnected] Cannot reach agent backend', '#e74c3c');
		}
	}

	// ─── WebSocket ──────────────────────────────────────────────────

	private connectToAgentBackend(): void {
		try {
			this.ws = new WebSocket('ws://localhost:8765');
		} catch {
			this._addLogEntry('[error] Failed to create WebSocket', '#e74c3c');
			return;
		}

		this.ws.onopen = () => {
			this.reconnectDelay = 1000; // reset backoff on success
			if (this.statusText) {
				this.statusText.textContent = localize('pulseAgent.status.ready', 'Ready');
				this.statusText.style.color = '#4caf50';
			}
			if (this.statusDot) {
				this.statusDot.style.background = '#4caf50';
			}
			this._addLogEntry('[connected] Pulse Agent backend online', '#4caf50');
		};

		this.ws.onmessage = (event: MessageEvent) => {
			this.stopThinking();
			const data = typeof event.data === 'string' ? event.data : String(event.data);
			this._addLogEntry(data, '#e0e0e0');
		};

		this.ws.onerror = () => {
			// silently ignored — onclose handles reconnect
		};

		this.ws.onclose = () => {
			if (this.disposed) {
				return;
			}
			if (this.statusText) {
				this.statusText.textContent = localize('pulseAgent.status.disconnected', 'Disconnected');
				this.statusText.style.color = '#e74c3c';
			}
			if (this.statusDot) {
				this.statusDot.style.background = '#e74c3c';
			}
			this._addLogEntry(`[disconnected] Agent offline — retry in ${this.reconnectDelay / 1000}s`, '#e74c3c');
			const delay = this.reconnectDelay;
			this.reconnectDelay = Math.min(this.reconnectDelay * 2, 30000);
			setTimeout(() => this.connectToAgentBackend(), delay);
		};
	}

	// ─── Dispose ───────────────────────────────────────────────────────

	override dispose(): void {
		this.disposed = true;
		this.stopThinking();
		if (this.ws) {
			this.ws.onclose = null; // prevent auto-reconnect on dispose
			this.ws.close();
			this.ws = null;
		}
		super.dispose();
	}
}
