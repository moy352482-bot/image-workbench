/* Prices come only from /api/scripts/config, backed by the Pricing Registry. */
(() => {
  'use strict';
  const priceText = value => Number.isInteger(value) ? (value === 0 ? '免费' : `${value} 积分`) : '暂未定价';
  function estimate(config, modelId, effort, credits, generationType) {
    if (!modelId || modelId === 'recommended') return null;
    const model = config?.models?.find(item => item.model_id === modelId);
    const level = model?.thinking_levels?.find(item => item.id === effort);
    const remaining = credits?.is_unlimited ? null : credits?.remaining_credit ?? config?.credits?.remaining_credit;
    let reason = '';
    if (!model || model.available === false) reason = model?.unavailable_reason || '当前模型不可用';
    else if (model.capabilities && !model.capabilities.includes(generationType)) reason = '当前模型不支持此内容类型';
    else if (!level) reason = '当前模型不支持此思考强度';
    else if (!Number.isInteger(level.price) || level.price < 0) reason = '此思考强度暂未定价';
    const price = level?.price;
    const shortfall = Number.isInteger(price) && Number.isFinite(remaining) ? Math.max(0, price - remaining) : 0;
    return { model, level, price, remaining, shortfall, reason, can_generate: !reason && shortfall === 0 };
  }
  function summary(value) {
    if (!value) return '填写需求后自动显示推荐模型与积分';
    if (value.reason) return value.reason + '，请选择可用模型与思考强度。';
    const balance = value.remaining === null ? '无限' : Number.isFinite(value.remaining) ? `${value.remaining} 积分` : '正在读取';
    return `${priceText(value.price)}/条 · 本次 1 条：${priceText(value.price)} · 当前余额：${balance}` +
      (value.shortfall ? `。积分不足，本次需要 ${value.price} 积分，当前剩余 ${value.remaining} 积分。` : '');
  }
  window.IWScriptPricing = Object.freeze({ priceText, estimate, summary });
})();
