/*---------------------------------------------------------------------------------------------
 *  Copyright (c) PulseCode AI. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { Codicon } from '../../../../base/common/codicons.js';
import { localize } from '../../../../nls.js';
import { localize2 } from '../../../../nls.js';
import { SyncDescriptor } from '../../../../platform/instantiation/common/descriptors.js';
import { registerIcon } from '../../../../platform/theme/common/iconRegistry.js';
import { Registry } from '../../../../platform/registry/common/platform.js';
import { ViewPaneContainer } from '../../../browser/parts/views/viewPaneContainer.js';
import { IViewContainersRegistry, IViewDescriptor, IViewsRegistry, ViewContainer, ViewContainerLocation, Extensions as ViewExtensions } from '../../../common/views.js';
import { PulseAgentView, PulseAgentViewId } from './pulseAgentView.js';

// ─── Icon ─────────────────────────────────────────────────────────────

const pulseAgentIcon = registerIcon(
	'pulse-agent-view-icon',
	Codicon.heart,
	localize('pulseAgentViewIcon', 'View icon of the Pulse Agent view.')
);

// ─── Container Registration (Secondary / Auxiliary Side Bar) ───────────

const VIEW_CONTAINER_ID = 'workbench.view.pulseAgent.container';

const pulseAgentContainer: ViewContainer = Registry.as<IViewContainersRegistry>(
	ViewExtensions.ViewContainersRegistry
).registerViewContainer(
	{
		id: VIEW_CONTAINER_ID,
		title: localize2('pulseAgent.container.title', 'Pulse Agent'),
		icon: pulseAgentIcon,
		ctorDescriptor: new SyncDescriptor(ViewPaneContainer, [
			VIEW_CONTAINER_ID,
			{ mergeViewWithContainerWhenSingleView: true },
		]),
		storageId: VIEW_CONTAINER_ID,
		hideIfEmpty: true,
		order: 2,
	},
	ViewContainerLocation.AuxiliaryBar,
	{ isDefault: false, doNotRegisterOpenCommand: false }
);

// ─── View Descriptor ──────────────────────────────────────────────────

const pulseAgentViewDescriptor: IViewDescriptor = {
	id: PulseAgentViewId,
	containerIcon: pulseAgentContainer.icon,
	containerTitle: pulseAgentContainer.title.value,
	singleViewPaneContainerTitle: pulseAgentContainer.title.value,
	name: localize2('pulseAgent.view.name', 'Pulse Agent'),
	canToggleVisibility: false,
	canMoveView: true,
	ctorDescriptor: new SyncDescriptor(PulseAgentView),
};

Registry.as<IViewsRegistry>(ViewExtensions.ViewsRegistry).registerViews(
	[pulseAgentViewDescriptor],
	pulseAgentContainer
);
