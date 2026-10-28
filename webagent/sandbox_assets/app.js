/* Product pages; grading stays server-side. */
(() => {
  'use strict';
  const CART_KEY = 'shoptrace.browser.sandbox.cart.v1';
  const categories = {headphones:'Headphones',thermos:'Thermos',powerbank:'Power bank',lamp:'Lamp'};
  const labels = {color:'Color',wireless:'Wireless',connection:'Connection',capacity_ml:'Capacity',capacity_mah:'Nominal capacity',material:'Material',max_output_w:'Maximum output',port:'Port',weight_g:'Weight',dimmable:'Dimmable',light:'Light',power_supply:'Power source',brightness_levels:'Brightness levels'};
  const text = (tag, value, cls) => {const node=document.createElement(tag); if(value!==undefined) node.textContent=value; if(cls)node.className=cls; return node;};
  const content=document.getElementById('content');
  let products=[];
  function money(value){return `CNY ${Number(value).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}`;}
  function link(label,href,cls){const node=text('a',label,cls);node.href=href;return node;}
  function getProduct(id){return products.find(item=>item.id===id);}
  function pickOpts(product,selections={}){const out={};for(const axis of product.options){const value=selections[axis.id]??axis.values[0].value;if(!axis.values.some(item=>item.value===value))throw new Error('invalid variant');out[axis.id]=value;}return out;}
  function variant(product,selections={}){const selected=pickOpts(product,selections),facts={...product.facts};let price=product.price;for(const axis of product.options){const option=axis.values.find(item=>item.value===selected[axis.id]);price+=option.price_delta||0;if(axis.id==='color')facts.color=option.value;if(axis.id==='capacity')facts.capacity_ml=Number.parseInt(option.value,10);}return{selected,price,facts};}
  function fixColor(product,selected={}){
    if(!selected||typeof selected!=='object'||Array.isArray(selected))throw new Error('invalid variant');
    const oldColors={'\u9ed1\u8272':'Black','\u7070\u8272':'Gray','\u767d\u8272':'White','\u84dd\u8272':'Blue','\u7c89\u8272':'Pink','\u7c73\u767d\u8272':'Ivory','\u7eff\u8272':'Green','\u7d2b\u8272':'Purple','\u7ea2\u8272':'Red','\u94f6\u8272':'Silver'};
    if(!Object.hasOwn(oldColors,selected.color))return selected;
    const value=oldColors[selected.color],axis=product.options.find(item=>item.id==='color');
    if(!axis||!axis.values.some(item=>item.value===value))return selected;
    return {...selected,color:value};
  }
  function readCart(){
    try{
      const rows=JSON.parse(localStorage.getItem(CART_KEY)||'[]');
      if(!Array.isArray(rows))return[];
      return rows.flatMap(row=>{
        try{
          const product=getProduct(row.productId);
          if(!product)return[];
          const selected=pickOpts(product,fixColor(product,row.selections));
          return [{productId:product.id,selections:selected}];
        }catch{return[];}
      });
    }catch{return[];}
  }
  function writeCart(rows){localStorage.setItem(CART_KEY,JSON.stringify(rows));countCart();}
  function countCart(){document.getElementById('cart-count').textContent=String(readCart().length);}
  function formatted(key,value){if(value===null||value===undefined)return 'Not provided';if(typeof value==='boolean')return value?'Yes':'No';if(key==='capacity_ml')return `${value} mL`;if(key==='capacity_mah')return `${Number(value).toLocaleString('en-US')} mAh`;if(key==='max_output_w')return `${value} W`;if(key==='weight_g')return `${value} g`;return String(value);}
  function factsBlock(facts){const dl=text('dl',undefined,'facts');for(const [key,value] of Object.entries(facts)){const row=text('div');row.append(text('dt',labels[key]||key),text('dd',formatted(key,value)));dl.append(row);}return dl;}
  function hero(title,subtitle){const section=text('section',undefined,'hero');section.append(text('p','Product details · check each requirement','eyebrow'),text('h1',title),text('p',subtitle));return section;}
  function drawIcon(category){const wrap=text('div',undefined,`illustration ${category}`);wrap.setAttribute('aria-hidden','true');const shapes={headphones:'♬',thermos:'▥',powerbank:'ϟ',lamp:'◒'};wrap.append(text('span',shapes[category]||'◇'),text('small','Product illustration'));return wrap;}
  function searchPage(){
    document.title='Browse products · ShopTrace Store';content.replaceChildren(hero('Choose a product','Check the visible details, price, and specifications. Missing information stays unknown.'));
    const params=new URLSearchParams(location.search),q=params.get('q')||'',category=params.get('category')||'',max=params.get('max_price')||'';
    const form=text('form',undefined,'search-form');form.action='/sandbox/';form.method='get';
    const queryLabel=text('label','Search products');const query=text('input');query.name='q';query.id='search-query';query.type='search';query.value=q;query.placeholder='For example, headphones or thermos';queryLabel.append(query);
    const catLabel=text('label','Category');const select=text('select');select.name='category';select.id='category-filter';select.setAttribute('aria-label','Category');for(const [value,label] of [['','All categories'],...Object.entries(categories)]){const option=text('option',label);option.value=value;option.selected=value===category;select.append(option);}catLabel.append(select);
    const priceLabel=text('label','Maximum price (CNY)');const price=text('input');price.type='number';price.min='0';price.step='0.01';price.name='max_price';price.id='budget-max';price.value=max;price.placeholder='No limit';priceLabel.append(price);
    const submit=text('button','Search','primary');submit.type='submit';form.append(queryLabel,catLabel,priceLabel,submit);content.append(form);
    const terms=q.toLocaleLowerCase('en-US').trim().split(/\s+/).filter(Boolean),maxNumber=max===''?Infinity:Number(max);
    const filtered=products.filter(p=>{const haystack=[p.name,categories[p.category],...p.tags,...Object.entries(p.facts).map(([key,value])=>formatted(key,value))].join(' ').toLocaleLowerCase('en-US');return(!category||p.category===category)&&(!Number.isFinite(maxNumber)||p.price<=maxNumber)&&terms.every(term=>haystack.includes(term));});
    const pageSize=2,totalPages=Math.max(1,Math.ceil(filtered.length/pageSize)),page=Math.max(1,Math.min(totalPages,Number.parseInt(params.get('page')||'1',10)||1));
    const result=text('section',undefined,'results');result.id='results';result.dataset.totalResults=String(filtered.length);result.dataset.searchQuery=q;result.dataset.category=category;result.dataset.maxPrice=max;
    const heading=text('div',undefined,'result-heading');heading.append(text('h2',`${filtered.length} ${filtered.length===1?'product':'products'} found`),text('p',`Page ${page} of ${totalPages} · ${pageSize} per page`));result.append(heading);
    if(!filtered.length){const empty=text('div',undefined,'empty');empty.append(text('h3','No products match these filters'),text('p','Try adjusting the filters.'));result.append(empty);}
    const grid=text('div',undefined,'product-grid');for(const product of filtered.slice((page-1)*pageSize,page*pageSize)){const card=text('article',undefined,'product-card');card.dataset.productId=product.id;const body=text('div',undefined,'card-body');body.append(text('span',categories[product.category],'category'),link(product.name,`/sandbox/product?id=${encodeURIComponent(product.id)}`,'product-title'),text('p',product.summary),text('strong',`${money(product.price)}${product.options.length?' and up':''}`,'price'),link('View details',`/sandbox/product?id=${encodeURIComponent(product.id)}`,'button secondary'));card.append(drawIcon(product.category),body);grid.append(card);}result.append(grid);
    if(totalPages>1){const navigation=text('nav',undefined,'pagination');navigation.setAttribute('aria-label','Product pagination');if(page>1){const previous=new URLSearchParams(params);previous.set('page',String(page-1));navigation.append(link('Previous page',`/sandbox/?${previous}`,'button secondary'));}navigation.append(text('span',`Page ${page}`));if(page<totalPages){const next=new URLSearchParams(params);next.set('page',String(page+1));navigation.append(link('Next page',`/sandbox/?${next}`,'button secondary'));}result.append(navigation);}
    content.append(result);
  }
  function detailPage(){
    const id=new URLSearchParams(location.search).get('id'),product=getProduct(id);if(!product){content.replaceChildren(hero('Product not found','Return to the product list and choose another item.'),link('Back to products','/sandbox/','button secondary'));return;}
    document.title=`${product.name} · ShopTrace Store`;content.replaceChildren(link('← Back to products','/sandbox/','back-link'));
    const section=text('section',undefined,'product-details');section.id='product-details';section.dataset.productId=product.id;const visual=drawIcon(product.category);const body=text('div',undefined,'detail-body');body.append(text('span',categories[product.category],'category'),text('h1',product.name),text('p',product.summary));const price=text('strong',undefined,'detail-price');price.id='variant-price';body.append(price);
    const selection=text('div',undefined,'variant-options');for(const axis of product.options){const label=text('label',axis.label),select=text('select');select.id=`option-${axis.id}`;select.name=axis.id;select.setAttribute('aria-label',axis.label);for(const item of axis.values){const option=text('option',item.label);option.value=item.value;select.append(option);}select.addEventListener('change',setVariant);label.append(select);selection.append(label);}body.append(selection);
    const facts=text('section',undefined,'fact-section');facts.append(text('h2','Product specifications'));const factBody=text('div');factBody.id='product-facts';facts.append(factBody);body.append(facts);
    const status=text('p',undefined,'add-status');status.id='add-status';status.setAttribute('role','status');
    const add=text('button','Add to list','button primary');add.type='button';add.id='add-to-cart';add.addEventListener('click',()=>{try{const current=variant(product,curOptions()),rows=readCart().filter(row=>row.productId!==product.id);rows.push({productId:product.id,selections:current.selected});writeCart(rows);status.textContent='Added to the list. Open it to check the final variant and price.';}catch{status.textContent='Browser storage is unavailable. The item was not added.';}});
    const actions=text('div',undefined,'detail-actions');actions.append(add,link('View list','/sandbox/cart','button secondary'));body.append(actions,status,text('p','Open the list to review the item and variant.','local-note'));section.append(visual,body);content.append(section);
    function curOptions(){return Object.fromEntries(product.options.map(axis=>[axis.id,document.getElementById(`option-${axis.id}`).value]));}
    function setVariant(){const current=variant(product,curOptions());price.textContent=money(current.price);price.dataset.unitPrice=String(current.price);factBody.replaceChildren(factsBlock(current.facts));section.dataset.variantColor=current.facts.color||'';section.dataset.variantCapacity=current.facts.capacity_ml?`${current.facts.capacity_ml}ml`:'';}
    setVariant();
  }
  function cartPage(){
    document.title='Shopping list · ShopTrace Store';content.replaceChildren(hero('Review your shopping list','Review the items, selected variants, and final prices. This page has no checkout.'));
    const rows=readCart(),list=text('section',undefined,'cart-list');list.id='cart-items';
    if(!rows.length){const empty=text('div',undefined,'empty');empty.append(text('h2','Your shopping list is empty'),link('Browse products','/sandbox/','button secondary'));list.append(empty);}
    for(const row of rows){const product=getProduct(row.productId),current=variant(product,row.selections),card=text('article',undefined,'cart-item');card.dataset.productId=product.id;card.dataset.variantColor=current.facts.color||'';card.dataset.variantCapacity=current.facts.capacity_ml?`${current.facts.capacity_ml}ml`:'';card.dataset.unitPrice=String(current.price);const body=text('div',undefined,'cart-item-body');body.append(link(product.name,`/sandbox/product?id=${encodeURIComponent(product.id)}`,'product-title'),factsBlock(current.facts),text('p','Quantity: 1','quantity'));const end=text('div',undefined,'cart-item-end');end.append(text('strong',money(current.price),'price'));const remove=text('button','Remove','text-button');remove.type='button';remove.setAttribute('aria-label',`Remove ${product.name}`);remove.addEventListener('click',()=>{writeCart(readCart().filter(item=>item.productId!==product.id));cartPage();});end.append(remove);card.append(drawIcon(product.category),body,end);list.append(card);}
    content.append(list);if(rows.length){const total=rows.reduce((sum,row)=>sum+variant(getProduct(row.productId),row.selections).price,0),summary=text('div',undefined,'cart-summary');summary.append(text('p',`${rows.length} ${rows.length===1?'item':'items'}`),text('strong',`Total ${money(total)}`));const clear=text('button','Clear list','button secondary');clear.type='button';clear.addEventListener('click',()=>{writeCart([]);cartPage();});summary.append(clear);content.append(summary);}
    content.append(text('p','Missing information stays unknown.','local-note'));
  }
  async function start(){try{const response=await fetch('/sandbox/assets/catalog.json');if(!response.ok)throw new Error('catalog unavailable');const catalog=await response.json();products=catalog.products;countCart();if(location.pathname.replace(/\/$/,'')==='/sandbox/product')detailPage();else if(location.pathname.replace(/\/$/,'')==='/sandbox/cart')cartPage();else searchPage();}catch{content.replaceChildren(hero('Product details could not be loaded','Check the local store service and refresh.'));}}
  start();
})();
