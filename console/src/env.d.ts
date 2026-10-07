/** Set by vite.config.ts from VITE_SSC_DEV_LOGIN; see src/auth/flags.ts. */
declare const __SSC_DEV_LOGIN__: boolean;

interface ImportMetaEnv {
  /** The auth host's origin, https://auth.delimitus.com when unset; see src/auth/oauth.ts. */
  readonly VITE_SSC_AUTH_URL?: string;
  /** The resource the console asks for, the API's user audience; https://api.delimitus.com. */
  readonly VITE_SSC_API_AUDIENCE?: string;
}
