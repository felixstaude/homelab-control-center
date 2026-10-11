import { Link } from 'react-router-dom';

import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';

export function NotFoundPage() {
  return (
    <Card>
      <CardContent className="flex flex-col items-center px-6 py-16 text-center">
        <p className="text-text-faint font-mono text-sm">404</p>
        <h2 className="text-text-primary mt-3 text-lg font-semibold">Page not found</h2>
        <p className="text-text-muted mt-2 max-w-sm text-sm leading-relaxed">
          This route does not exist. If you followed a link from inside the
          application, that is a bug worth reporting.
        </p>
        <Button asChild variant="primary" className="mt-6">
          <Link to="/">Back to overview</Link>
        </Button>
      </CardContent>
    </Card>
  );
}
