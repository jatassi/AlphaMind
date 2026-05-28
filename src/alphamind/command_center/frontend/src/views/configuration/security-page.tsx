// SecurityConfigPage — config/security.yaml editor (story 06b / ALP-683).
//
// Thin scaffold wrapper — security.yaml carries no hot-reload-engine
// machinery beyond what the framework's ReloadPolicyBadge already
// surfaces (webauthn.relying_party_id is DEPLOY_TIME; for LAN access it must
// match the `access:` host per ALP-724 / RUNBOOK_command_center.md).

import { ConfigEditorPage } from '@/views/configuration/config-editor-page'

export function SecurityConfigPage(): React.JSX.Element {
  return <ConfigEditorPage configFileSlug="security" title="Security configuration" />
}
