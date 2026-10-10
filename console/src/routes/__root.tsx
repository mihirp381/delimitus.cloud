import { createRootRouteWithContext, Link, Outlet } from '@tanstack/react-router';
import { Brand, BrandRule } from '../components/Brand';
import type { RouterContext } from '../context';

export const Route = createRootRouteWithContext<RouterContext>()({
  component: () => <Outlet />,
  notFoundComponent: NotFound,
});

function NotFound() {
  return (
    <main className="login">
      <div className="login-card">
        <BrandRule />
        <span className="brand">
          <Brand />
        </span>
        <h1>Page not found</h1>
        <p className="lede">Nothing lives at this address. It may have moved, or the link may be mistyped.</p>
        <p>
          <Link to="/" className="btn btn-secondary">
            Back to apps
          </Link>
        </p>
      </div>
    </main>
  );
}
