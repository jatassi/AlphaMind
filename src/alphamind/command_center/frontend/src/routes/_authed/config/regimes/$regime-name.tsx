// Regime editor route — /config/regimes/$regimeName (ALP-682).
// Inherits the protected AuthedLayout (Nav + AlertBanner) from _authed.tsx.
import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '@/routes/_authed'
import { RegimeEditorPage } from '@/views/configuration/regimes/regime-editor-page'

export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/config/regimes/$regimeName',
  component: function RegimeNamePage() {
    const { regimeName } = Route.useParams()
    return <RegimeEditorPage regimeName={regimeName} />
  },
})
