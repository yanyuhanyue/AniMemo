import { Icon } from './ui/Icon';
import { ratingTier } from './ratingScale';
import './rating.css';

export function Rating({ score, label = '我的评分', className = '' }: { score: number | null; label?: string; className?: string }) {
  return <span className={`rating rating--${ratingTier(score)} ${className}`} aria-label={score === null ? '未评分' : `${label} ${score.toFixed(1)} 分`}>
    {score !== null && <Icon name="star" />}
    <strong className="rating-value">{score === null ? '未评分' : score.toFixed(1)}</strong>
    {score !== null && <small>/10</small>}
  </span>;
}
