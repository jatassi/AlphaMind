// DigestConfigPage — config/digest.yaml editor (story 06b / ALP-683).
//
// Notable-shift thresholds the weekly digest renders against.

import { ConfigEditorPage } from '@/views/configuration/config-editor-page'

export function DigestConfigPage(): React.JSX.Element {
  return <ConfigEditorPage configFileSlug="digest" title="Weekly digest thresholds" />
}
