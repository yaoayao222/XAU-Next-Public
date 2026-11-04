import createClient from 'openapi-fetch';
import type { paths } from './schema';
export type { components, paths, operations } from './schema';
export const createApiClient = (baseUrl: string, token: string) => createClient<paths>({baseUrl, headers: {Authorization: `Bearer ${token}`}});
