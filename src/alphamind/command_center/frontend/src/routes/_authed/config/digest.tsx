import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '@/routes/_authed'
import { DigestConfigPage } from '@/views/configuration/digest-page'

// /config/digest — notable-shift threshold editor for the weekly digest
// (story 06b / ALP-683).
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/config/digest',
  component: DigestConfigPage,
})
