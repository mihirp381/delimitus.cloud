/**
 * The brand: the Delimitus mark, drawn in the text colour as in the landing page's footer, and
 * the console's name. The caller wraps it in a link where there is somewhere to go.
 */
export function Brand() {
  return (
    <>
      <svg className="brand-mark" viewBox="0 0 22 22" aria-hidden="true" focusable="false">
        <path
          fill="currentColor"
          fillRule="evenodd"
          d="M6 3H11.5A8 8 0 0 1 11.5 19H6A2 2 0 0 1 4 17V5A2 2 0 0 1 6 3ZM7.52 16L10.16 16A0.35 0.35 0 0 0 10.49 15.78L14.25 6.48A0.35 0.35 0 0 0 13.92 6L11.28 6A0.35 0.35 0 0 0 10.95 6.22L7.19 15.52A0.35 0.35 0 0 0 7.52 16Z"
        />
      </svg>
      <span>SSC console</span>
    </>
  );
}

/**
 * The spectrum, the one brand mark: a thin rule under the top bar, or across the top of a
 * sign-in card. It appears once on a screen and nowhere else.
 */
export function BrandRule() {
  return <span className="brand-rule" aria-hidden="true" />;
}
