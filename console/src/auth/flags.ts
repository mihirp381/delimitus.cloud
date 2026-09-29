/**
 * True in `vite` dev and in builds made with VITE_SSC_DEV_LOGIN=1; a literal `false` in any
 * other production build, so the bundler drops the dev login. `test/devlogin-absent.test.ts`
 * checks the build output.
 */
export const DEV_LOGIN: boolean = import.meta.env.DEV || __SSC_DEV_LOGIN__;
