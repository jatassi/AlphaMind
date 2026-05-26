import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '@/routes/_authed'
import { CommandCenterConfigPage } from '@/views/configuration/command-center-page'

// /config/command-center — bind / DB / frontend dist / upstream URLs editor
// (story 06b / ALP-683).
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/config/command-center',
  component: CommandCenterConfigPage,
})
