import { useQuery } from '@tanstack/react-query';
import { api } from './client';

export function keepNewer<T>(previous: T | undefined, next: T): T {
  if (!previous || !next || typeof previous !== 'object' || typeof next !== 'object') return next;
  const a = previous as { revision?: number; event_cursor?: number };
  const b = next as { revision?: number; event_cursor?: number };
  if (a.event_cursor !== undefined && b.event_cursor !== undefined && a.event_cursor > b.event_cursor) return previous;
  if (a.revision !== undefined && b.revision !== undefined && a.revision > b.revision) return previous;
  return next;
}
export const useWorkspace = () => useQuery({ queryKey: ['workspace'], queryFn: api.workspace, refetchInterval: 30000, structuralSharing: keepNewer });
export const useSettings = () => useQuery({ queryKey: ['settings'], queryFn: api.settings, structuralSharing: keepNewer });
