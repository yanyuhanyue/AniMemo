import { Icon } from './ui/Icon';
import { ratingTier } from './ratingScale';
import './rating.css';

export function Rating({ score, label = '我的评分', variant = 'inline', className = '' }: { score: number | null; label?: string; variant?: 'inline' | 'badge'; className?: string }) {
  const description = score === null ? '未评分' : `${label} ${score.toFixed(1)} 分`;
  return <span className={`rating rating--${ratingTier(score)} rating--${variant} ${className}`} aria-label={description} title={description}>
    {score !== null && variant === 'badge' && <Icon name="star" />}
    <strong className="rating-value">{score === null ? '未评分' : score.toFixed(1)}</strong>
    {score !== null && variant === 'inline' && <small>分</small>}
  </span>;
}
