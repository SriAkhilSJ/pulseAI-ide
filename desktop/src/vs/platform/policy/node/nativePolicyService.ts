/// <reference types="node" />

/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { AbstractPolicyService, IPolicyService } from '../common/policy.js';
import { IStringDictionary } from '../../../base/common/collections.js';
import { Throttler } from '../../../base/common/async.js';
import { MutableDisposable } from '../../../base/common/lifecycle.js';
import { ILogService } from '../../log/common/log.js';

export class NativePolicyService extends AbstractPolicyService implements IPolicyService {

	private throttler = this._register(new Throttler());
	private readonly watcher = this._register(new MutableDisposable());

	private readonly _isPolicyDisabled: boolean;

	constructor(
		@ILogService private readonly logService: ILogService,
		private readonly productName: string
	) {
		super();
		const isProcessAvailable = typeof process !== 'undefined' && process.env;
		const disableFlag = isProcessAvailable ? process.env.VSCODE_POLICY_DISABLED : undefined;
		this._isPolicyDisabled = !!(disableFlag);
		this.logService.info(`NativePolicyService: process.env.VSCODE_POLICY_DISABLED = ${disableFlag}, _isPolicyDisabled = ${this._isPolicyDisabled}`);
	}

	protected async _updatePolicyDefinitions(policyDefinitions: IStringDictionary<any>): Promise<void> {
		if (this._isPolicyDisabled) {
			this.logService.trace('NativePolicyService#_updatePolicyDefinitions - Skipping due to VSCODE_POLICY_DISABLED');
			return;
		}

		this.logService.trace(`NativePolicyService#_updatePolicyDefinitions - Found ${Object.keys(policyDefinitions).length} policy definitions`);

		let createWatcher;
		try {
			const policyWatcher = await import('@vscode/policy-watcher');
			createWatcher = policyWatcher.createWatcher;
		} catch (err) {
			this.logService.error('Failed to load @vscode/policy-watcher: ', err);
			// Provide a no-op watcher that implements the minimal interface
			createWatcher = () => {
				return {
					dispose: () => {}
				};
			};
		}

		await this.throttler.queue(() => new Promise<void>((c, e) => {
			try {
				this.logService.trace(`Creating watcher for productName ${this.productName}`);
				this.watcher.value = createWatcher(this.productName, policyDefinitions, update => {
					this._onDidPolicyChange(update);
					c();
				});
			} catch (err) {
				this.logService.error(`NativePolicyService#_updatePolicyDefinitions - Error creating watcher:`, err);
				e(err);
			}
		}));
	}

	private _onDidPolicyChange(update: any): void {
		this.logService.trace(`NativePolicyService#_onDidPolicyChange - Updated policy values: ${JSON.stringify(update)}`);

		for (const key in update) {
			const value = update[key];

			if (value === undefined) {
				this.policies.delete(key);
			} else {
				this.policies.set(key, value);
			}
		}

		this._onDidChange.fire(Object.keys(update));
	}
}