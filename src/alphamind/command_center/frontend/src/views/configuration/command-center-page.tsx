// CommandCenterConfigPage — config/command-center.yaml editor (story 06b /
// ALP-683). The bind / DB-path / frontend-dist sub-fields carry
// DEPLOY_TIME badges via the schema endpoint.

import { ConfigEditorPage } from '@/views/configuration/config-editor-page'

export function CommandCenterConfigPage(): React.JSX.Element {
  return <ConfigEditorPage configFileSlug="command-center" title="Command center configuration" />
}
