// Invented product facts for the participation demo.
const sceneData = [
  {
    id: 'commute-headphones',
    version: '1.0',
    title: 'Choose commuting headphones',
    subtitle: 'Compare the budget, color and connection type',
    icon: '🎧',
    need: 'I want black wireless headphones for commuting, priced at CNY 300 or less.',
    disclosure: 'These products, prices and page excerpts are fictional. No live search or purchase takes place.',
    requirements: [
      { id: 'budget', text: 'Price is CNY 300 or less', query_quote: 'priced at CNY 300 or less' },
      { id: 'color', text: 'Color is black', query_quote: 'black' },
      { id: 'wireless', text: 'Supports wireless connectivity', query_quote: 'wireless headphones' },
    ],
    candidates: [
      {
        id: 'a', name: 'Sample Commute headphones', brand: 'Demo', price: 299, color: 'Black',
        specs: [
          { label: 'Listed price', value: 'CNY 299' },
          { label: 'Color', value: 'Black' },
          { label: 'Connection', value: 'Bluetooth wireless' },
        ],
        evidence: [
          { id: 'headphones-a-price', text: 'The sample page lists a price of CNY 299.', requirement_ids: ['budget'] },
          { id: 'headphones-a-color', text: 'The sample page shows the color as black.', requirement_ids: ['color'] },
          { id: 'headphones-a-wireless', text: 'The specifications list Bluetooth wireless connectivity.', requirement_ids: ['wireless'] },
        ],
      },
      {
        id: 'b', name: 'Sample Daytrip headphones', brand: 'Demo', price: 269, color: 'Gray',
        specs: [
          { label: 'Listed price', value: 'CNY 269' },
          { label: 'Color', value: 'Gray' },
          { label: 'Connection', value: 'Not shown' },
        ],
        evidence: [
          { id: 'headphones-b-price', text: 'The sample page lists a price of CNY 269.', requirement_ids: ['budget'] },
          { id: 'headphones-b-color', text: 'The sample page shows the color as gray.', requirement_ids: ['color'] },
          { id: 'headphones-b-wireless', text: 'The page provides no connection type or wireless feature information.', requirement_ids: ['wireless'] },
        ],
      },
    ],
    steps: [
      { title: 'Identify the requirements', text: 'Take the budget, color and connection type from the request as three conditions to check.', evidence_ids: [] },
      { title: 'Compare the page information', text: 'Place the prices, colors and connection types side by side. Keep missing fields marked as missing.', evidence_ids: ['headphones-a-price', 'headphones-a-color', 'headphones-a-wireless', 'headphones-b-price', 'headphones-b-color', 'headphones-b-wireless'] },
      { title: 'Check each condition', text: 'Review the evidence and choose meets, does not meet or not enough information. This demo runs no model and does not score your judgment.', evidence_ids: [] },
    ],
  },
  {
    id: 'daily-thermos',
    version: '1.0',
    title: 'Choose an everyday thermos',
    subtitle: 'Check the price, color and exact capacity',
    icon: '🥤',
    need: 'I want a blue 500ml thermos priced at CNY 100 or less.',
    disclosure: 'These products, prices and page excerpts are fictional. No live search or purchase takes place.',
    requirements: [
      { id: 'budget', text: 'Price is CNY 100 or less', query_quote: 'priced at CNY 100 or less' },
      { id: 'color', text: 'Color is blue', query_quote: 'blue' },
      { id: 'capacity', text: 'Capacity is 500ml', query_quote: '500ml thermos' },
    ],
    candidates: [
      {
        id: 'a', name: 'Sample Blue Bay thermos', brand: 'Demo', price: 89, color: 'Blue',
        specs: [
          { label: 'Listed price', value: 'CNY 89' },
          { label: 'Color', value: 'Blue' },
          { label: 'Capacity', value: '450ml' },
        ],
        evidence: [
          { id: 'thermos-a-price', text: 'The sample page lists a price of CNY 89.', requirement_ids: ['budget'] },
          { id: 'thermos-a-color', text: 'The sample page shows the color as blue.', requirement_ids: ['color'] },
          { id: 'thermos-a-capacity', text: 'The specifications list a capacity of 450ml.', requirement_ids: ['capacity'] },
        ],
      },
      {
        id: 'b', name: 'Sample Light Sip thermos', brand: 'Demo', price: 109,
        specs: [
          { label: 'Listed price', value: 'CNY 109' },
          { label: 'Color', value: 'Not shown' },
          { label: 'Capacity', value: '500ml' },
        ],
        evidence: [
          { id: 'thermos-b-price', text: 'The sample page lists a price of CNY 109.', requirement_ids: ['budget'] },
          { id: 'thermos-b-color', text: 'The page provides no color name or color options.', requirement_ids: ['color'] },
          { id: 'thermos-b-capacity', text: 'The specifications list a capacity of 500ml.', requirement_ids: ['capacity'] },
        ],
      },
    ],
    steps: [
      { title: 'Identify the requirements', text: 'Keep price, color and capacity as separate conditions, including the exact 500ml requirement.', evidence_ids: [] },
      { title: 'Compare the page information', text: 'List both thermoses and their price, color and capacity. Record a missing color without guessing.', evidence_ids: ['thermos-a-price', 'thermos-a-color', 'thermos-a-capacity', 'thermos-b-price', 'thermos-b-color', 'thermos-b-capacity'] },
      { title: 'Check each condition', text: 'Review all three conditions, then share your overall preference. No preferred answer is supplied.', evidence_ids: [] },
    ],
  },
  {
    id: 'travel-power-bank',
    version: '1.0',
    title: 'Compare travel power banks',
    subtitle: 'Check capacity and notice missing travel markings',
    icon: '🔋',
    need: 'I want a 10000mAh power bank for a business trip, priced at CNY 150 or less. The page must clearly show aircraft-carry marking information.',
    disclosure: 'These products, prices and page excerpts are fictional. We check only whether a marking is displayed, not actual airline eligibility.',
    requirements: [
      { id: 'budget', text: 'Price is CNY 150 or less', query_quote: 'priced at CNY 150 or less' },
      { id: 'capacity', text: 'Capacity is 10000mAh', query_quote: '10000mAh power bank' },
      { id: 'marking', text: 'The page shows aircraft-carry marking information', query_quote: 'The page must clearly show aircraft-carry marking information' },
    ],
    candidates: [
      {
        id: 'a', name: 'Sample Long Trip power bank', brand: 'Demo', price: 129,
        specs: [
          { label: 'Listed price', value: 'CNY 129' },
          { label: 'Rated capacity', value: '20000mAh' },
          { label: 'Aircraft-carry marking', value: 'Not shown' },
        ],
        evidence: [
          { id: 'power-a-price', text: 'The sample page lists a price of CNY 129.', requirement_ids: ['budget'] },
          { id: 'power-a-capacity', text: 'The specifications list a rated capacity of 20000mAh.', requirement_ids: ['capacity'] },
          { id: 'power-a-marking', text: 'The page provides no aircraft-carry marking information.', requirement_ids: ['marking'] },
        ],
      },
      {
        id: 'b', name: 'Sample Travel Lite power bank', brand: 'Demo', price: 169,
        specs: [
          { label: 'Listed price', value: 'CNY 169' },
          { label: 'Rated capacity', value: '10000mAh' },
          { label: 'Aircraft-carry marking', value: 'Sample marking information shown' },
        ],
        evidence: [
          { id: 'power-b-price', text: 'The sample page lists a price of CNY 169.', requirement_ids: ['budget'] },
          { id: 'power-b-capacity', text: 'The specifications list a rated capacity of 10000mAh.', requirement_ids: ['capacity'] },
          { id: 'power-b-marking', text: 'The page includes a sample aircraft-carry marking section. This records its presence only, without verifying the marking or airline eligibility.', requirement_ids: ['marking'] },
        ],
      },
    ],
    steps: [
      { title: 'Identify the requirements', text: 'Separate the budget, capacity and displayed marking requirements. Check only the information on the page.', evidence_ids: [] },
      { title: 'Compare the page information', text: 'List both power banks, their prices, rated capacities and whether marking information is shown.', evidence_ids: ['power-a-price', 'power-a-capacity', 'power-a-marking', 'power-b-price', 'power-b-capacity', 'power-b-marking'] },
      { title: 'Check each condition', text: 'Use the sample page evidence. A displayed marking does not establish airline eligibility or guarantee compliance with transport rules.', evidence_ids: [] },
    ],
  },
];

function deepFreeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.freeze(value);
    Object.values(value).forEach(deepFreeze);
  }
  return value;
}

export const scenarios = deepFreeze(sceneData);

export function getScenario(id) {
  return scenarios.find((scenario) => scenario.id === id);
}
