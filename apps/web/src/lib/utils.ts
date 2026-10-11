import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/**
 * Merge class names, resolving conflicting Tailwind utilities.
 *
 * `clsx` handles conditionals; `twMerge` ensures a caller-supplied `p-6` wins
 * over a component's default `p-4` instead of both landing in the class list
 * with the winner decided by stylesheet order.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
