// Framework-level types (ALP-679).
//
// Mirrors the backend's Pydantic `FormSchema` + `ValidationReport`
// envelope so the frontend can type the form-schema endpoint response
// and the layered envelope returned by the PUT endpoint.

export type ControlType =
  | 'number'
  | 'boolean'
  | 'enum'
  | 'string'
  | 'string-array'
  | 'object-array'
  | 'cron'
  | 'path'

export type FormFieldSchema = {
  path: string
  control_type: ControlType
  reload_policy: 'invocation_time' | 'deploy_time'
  constraints: {
    minimum?: number
    maximum?: number
    minLength?: number
    maxLength?: number
    pattern?: string
    enum_choices?: readonly string[]
  }
  // Populated only for ``object-array`` controls — per-column
  // FormFieldSchema entries derived from the array element's
  // Pydantic model. ``null`` for non-typed list elements
  // (``list[dict[str, Any]]`` falls back to inferring columns from row
  // data in that case).
  columns?: readonly FormFieldSchema[] | null
}

export type FormSchema = {
  slug: string
  filename: string
  fields: readonly FormFieldSchema[]
}

export type ValidationLayerError = {
  path: string
  message: string
}

export type ValidationReport = {
  parse: readonly ValidationLayerError[]
  cross_reference: readonly ValidationLayerError[]
  semantic: readonly ValidationLayerError[]
}
