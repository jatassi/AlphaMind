import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '@/routes/_authed'
import { CalibrationMixPage } from '@/views/risk/calibration-mix-page'

// Calibration mix route — /risk/calibration-mix (ALP-680).
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/risk/calibration-mix',
  component: CalibrationMixPage,
})
