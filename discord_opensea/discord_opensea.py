import logging
from interactions import SlashContext
from interactions.models.discord import Embed, BrandColors, ButtonStyle, Button
import httpx
from discord_utils.discord_utils import truncate_string, format_time_remaining, format_datetime, mention
from discord_web3.discord_web3 import safe_to_wei, safe_to_ether, get_usd_price, tx_link_instruction_text, \
    get_tx_page_url, compress_string_to_url, generate_tx_key, format_wei_price, format_eth_price, get_contract
import discord_web3.discord_web3 as discord_web3
from decimal import Decimal, ROUND_DOWN
from db.tx_db import TxDB
from db.user_address_db import UserAddressDB
from web3 import Web3
from enum import Enum
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("discord_opensea")
http_client = httpx.AsyncClient()

opensea_urls = {
    "listings": "https://api.opensea.io/v2/orders/ethereum/seaport/listings",
    "fulfillment": "https://api.opensea.io/v2/listings/fulfillment_data",
    "offers": "https://api.opensea.io/v2/orders/ethereum/seaport/offers"
}

os_api_headers = {
    "accept": "application/json",
    "X-API-KEY": opensea_api_key,
    "content-type": "application/json"
}

contract_addresses = {
    "Seaport": "0x00000000000000ADc04C56Bf30aC9d3c0aAF14dC",
    "OpenSeaConduit": "0x1E0049783F008A0085193E00003D00cd54003c71",
    "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "DAI": "0x6B175474E89094C44Da98b954EedeAC495271d0F"
}

currency_choices = [{"name": "ETH", "value": "eth"},
                    {"name": "WETH", "value": "weth"},
                    {"name": "USDC", "value": "usdc"},
                    {"name": "DAI", "value": "dai"}]


class AssetType(Enum):
    NATIVE = 0
    ERC20 = 1
    ERC721 = 2
    ERC1155 = 3
    ERC721_WITH_CRITERIA = 4
    ERC1155_WITH_CRITERIA = 5


def get_asset_url(asset_contract_address, token_id):
    return f"https://opensea.io/assets/ethereum/{asset_contract_address}/{token_id}"


def get_user_url(address):
    return f"https://opensea.io/{address}"


def format_user_address_url(address):
    return f"[{truncate_string(address, 10)}]({get_user_url(address)})"


def generate_opensea_salt():
    salt_length = 77
    salt_min_value = 10 ** (salt_length - 1)
    salt_max_value = (10 ** salt_length) - 1
    return random.randint(salt_min_value, salt_max_value)


def prepare_tx_args(data):
    if isinstance(data, dict):
        # Prepare the dictionary items
        for key in data:
            if isinstance(data[key], str):
                if data[key].isdigit():
                    # Convert numeric strings to integers
                    data[key] = int(data[key])
                elif data[key].startswith('0x') and len(data[key]) == 42:
                    # Convert Ethereum addresses to checksum format
                    data[key] = Web3.to_checksum_address(data[key])
                    # Convert hex strings to bytes
                    # data[key] = bytes.fromhex(data[key][2:])
            elif isinstance(data[key], list):
                # Recursively prepare parameters in lists
                data[key] = [prepare_tx_args(param) if isinstance(param, dict) else param for param in
                             data[key]]

        # Extract values
        return tuple(prepare_tx_args(v) if isinstance(v, (dict, list)) else v for v in data.values())
    elif isinstance(data, list):
        return [prepare_tx_args(v) if isinstance(v, (dict, list)) else v for v in data]
    else:
        return data


def split_opensea_consideration(wei_value):
    # Split the provided wei_value into two parts: 97.5% and 2.5%
    part1 = int(wei_value * 0.975)
    part2 = int(wei_value * 0.025)

    return part1, part2


def restrict_to_multiples(value, multiple=0.0001):
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    decimal_multiple = Decimal(str(multiple))
    restricted_value = (value / decimal_multiple).quantize(Decimal('0'), rounding=ROUND_DOWN) * decimal_multiple
    return restricted_value


def get_token_contract(web3, token_address):
    token_mapping = {
        contract_addresses["USDC"].lower(): "USDC",
        contract_addresses["WETH"].lower(): "WETH",
        contract_addresses["DAI"].lower(): "DAI"
    }

    token_name = token_mapping.get(token_address.lower(), None)

    if token_name:
        token_name.lower(), get_contract(web3, token_address, token_name)
    else:
        return ("eth", None)


def get_button_id(asset_name, token_id):
    if len(token_id > 50) and asset_name is not None:
        return asset_name
    else:
        token_id


def get_token_address(token_name):
    token_mapping = {
        "eth": "0x0000000000000000000000000000000000000000",
        "usdc": usdc.address,
        "weth": weth.address,
        "dai": dai.address
    }
    return token_mapping[token_name]


def get_tx_data_from_fulfillment(fulfillment, order_type):
    fn_signature = fulfillment["fulfillment_data"]["transaction"]["function"]
    fn_name = fn_signature.split("(")[0]
    tx_value = int(fulfillment["fulfillment_data"]["transaction"]["value"])
    tx_to = Web3.to_checksum_address(fulfillment["fulfillment_data"]["transaction"]["to"])
    if order_type == "basic":
        parameters = fulfillment["fulfillment_data"]["transaction"]["input_data"]["parameters"]
        args = prepare_tx_args(parameters)
        tx_data = seaport.encodeABI(fn_name=fn_name, args=[list(args)])
    elif order_type == "dutch":
        order = fulfillment["fulfillment_data"]["transaction"]["input_data"]["order"]
        fulfiller_conduit_key = fulfillment["fulfillment_data"]["transaction"]["input_data"]["fulfillerConduitKey"]
        order_args = prepare_tx_args(order)
        tx_data = seaport.encodeABI(fn_name=fn_name, args=[list(order_args), fulfiller_conduit_key])
    return tx_data, tx_value, tx_to


async def get_highest_bid(asset_contract_address, token_id):
    url = f"{opensea_urls['offers']}?" \
          f"asset_contract_address={asset_contract_address}&" \
          f"token_ids={token_id}&" \
          f"order_by=eth_price&" \
          f"order_direction=desc&" \
          f"limit=1"

    response = await http_client.get(url, headers=os_api_headers)
    offers = response.json()

    if "orders" in offers and len(offers["orders"]) > 0:  # Have some bids in the auction
        offer = offers["orders"][0]["protocol_data"]["parameters"]["offer"][0]
        return int(offer["startAmount"]), offer["token"]
    return (None, None)


async def get_offers(asset_contract_address, token_id):
    url = f"{opensea_urls['offers']}?" \
          f"asset_contract_address={asset_contract_address}&" \
          f"token_ids={token_id}&" \
          f"order_by=eth_price&" \
          f"order_direction=desc&" \
          f"limit=5"

    response = await http_client.get(url, headers=os_api_headers)
    return response.json()


async def get_cheapest_listing(asset_contract_address, token_id):
    url = f"{opensea_urls['listings']}?" \
          f"asset_contract_address={asset_contract_address}&" \
          f"token_ids={token_id}&" \
          f"order_by=eth_price&" \
          f"order_direction=asc&" \
          f"limit=1"

    response = await http_client.get(url, headers=os_api_headers)

    listings = response.json()

    if not "orders" in listings or len(listings["orders"]) == 0:
        return None
    else:
        return listings["orders"][0]


async def get_fulfillment(order_hash, protocol_address, user_address):
    url = opensea_urls["fulfillment"]

    payload = {
        "listing": {
            "hash": order_hash,
            "chain": "ethereum",
            "protocol_address": protocol_address
        },
        "fulfiller": {
            "address": user_address
        }
    }

    response = await http_client.post(url, json=payload, headers=os_api_headers)
    return response.json()


def min_usd_worth(amount, min_usd_worth, eth_usd_price, token_name):
    """
    Checks if given amount is at least worth `min_usd_worth` in USD terms.
    Returns tuple, if it's worth and worth amount
    """
    if token_name == "usdc" or token_name == "dai":
        return (amount > min_usd_worth, amount)
    elif token_name == "eth" or token_name == "weth":
        usd_worth = amount * eth_usd_price
        return (usd_worth > min_usd_worth, usd_worth)
    else:
        return (False, 0)


def get_usd_worth_amount(usd_worth, eth_usd_price, token_name):
    """
    Calculates amount of token that's worth `usd_worth` in USD
    """
    usd_worth = Decimal(str(usd_worth))
    if token_name == "usdc" or token_name == "dai":
        return usd_worth
    elif token_name == "eth" or token_name == "weth":
        eth_amount = usd_worth / eth_usd_price
        return eth_amount
    else:
        0


def calculate_bid(current_price, bid_wei, highest_bid, token_name, eth_usd_price, force_highest_bid=False):
    if highest_bid:  # Have some bids in the auction
        if bid_wei and not force_highest_bid:
            if highest_bid >= bid_wei:
                return "too_low"
            else:  # user specified bid is good to go
                current_price = bid_wei
        else:  ## Auto-calculate adding 5% to the highest bid in a safe way
            new_current_price = restrict_to_multiples(safe_to_ether(highest_bid * 1.05, token_name))
            current_price = safe_to_wei(new_current_price, token_name)
    else:  # No bids in the auction
        if bid_wei:  # use user specified bid that has been checked above
            current_price = bid_wei
        else:  # No bids in the auction and no user specified bid
            is_worth, usd_worth = min_usd_worth(  # check if auction start price is 5 USD worth
                safe_to_ether(current_price, token_name), 5, eth_usd_price, token_name)
            if not is_worth:  # user didn't specify bid and auction start price is less than 5 USD
                desired_usd_worth = 5.01
                if token_name == "eth" or token_name == "weth":
                    desired_usd_worth = 5.1  # Add bit of a reserve
                new_current_price = get_usd_worth_amount(desired_usd_worth, eth_usd_price, token_name)
                current_price = safe_to_wei(restrict_to_multiples(new_current_price), token_name)
    return current_price


def get_order_start_end_times(duration_days):
    start_time = int(datetime.now().timestamp())
    end_time = int((datetime.now() + timedelta(days=duration_days)).timestamp())
    return (start_time, end_time)


def get_order_prices(start_price, end_price=None, unit="ether", token_name="eth"):
    if end_price is None:
        end_price = start_price

    if unit == "ether":
        start_price_wei = safe_to_wei(start_price, token_name)
    else:
        start_price_wei = start_price
    owner_start_price, os_fee_start_price = split_opensea_consideration(start_price_wei)
    if start_price == end_price:
        end_price_wei = start_price_wei
        owner_end_price = owner_start_price
        os_fee_end_price = os_fee_start_price
    else:
        if unit == "ether":
            end_price_wei = safe_to_wei(end_price, token_name)
        else:
            end_price_wei = end_price
        owner_end_price, os_fee_end_price = split_opensea_consideration(end_price_wei)

    return (owner_start_price, owner_end_price, os_fee_start_price, os_fee_end_price)


def get_order_parameters(offerer, offer_item_type, offer_token, offer_token_id, offer_start_amount, offer_end_amount,
                         cons_item_type, cons_token, cons_token_id, cons_start_amount, cons_end_amount, cons_recepient,
                         os_cons_item_type, os_cons_token, os_cons_start_amount, os_cons_end_amount, start_time,
                         end_time, order_type):
    return {
        "offerer": offerer,
        "offer": [{
            "itemType": offer_item_type,
            "token": offer_token,
            "identifierOrCriteria": str(offer_token_id),
            "startAmount": str(offer_start_amount),
            "endAmount": str(offer_end_amount)
        }],
        "consideration": [{
            "itemType": cons_item_type,
            "token": cons_token,
            "identifierOrCriteria": str(cons_token_id),
            "startAmount": str(cons_start_amount),
            "endAmount": str(cons_end_amount),
            "recipient": cons_recepient,
        }, {  ## OpenSea Fees have to be defined, at least 2.5%
            "itemType": os_cons_item_type,
            "token": os_cons_token,
            "identifierOrCriteria": 0,
            "startAmount": str(os_cons_start_amount),
            "endAmount": str(os_cons_end_amount),
            "recipient": "0x0000a26b00c1F0DF003000390027140000fAa719",
        }],
        "totalOriginalConsiderationItems": 2,
        "startTime": start_time,
        "endTime": end_time,
        "orderType": order_type,
        "zone": "0x004C00500000aD104D7DBd00e3ae0A5C00560C00",
        "zoneHash": "0x0000000000000000000000000000000000000000000000000000000000000000",
        "salt": str(generate_opensea_salt()),
        "conduitKey": "0x0000007b02230091a7ed01230072f7006a004d60a8d4e71d599b8104250f0000",
        "counter": 0,
    }


async def start_stream(bot, api_key, channel_id):
    try:
        logger.info("Starting OpenSea Stream")
        channel = bot.get_channel(channel_id)
        connection_string = f"wss://stream.openseabeta.com/socket/websocket?token={api_key}"
        async with websockets.connect(connection_string) as websocket:
            subscription_message = {
                "topic": f"collection:ens",
                "event": "phx_join",
                "payload": {},
                "ref": 0
            }
            await websocket.send(json.dumps(subscription_message))

            while True:
                response = await websocket.recv()
                response = json.loads(response)
                event = response["event"]
                payload = response.get("payload", {}).get("payload", {})
                if event == "item_received_bid":
                    await on_item_received_bid(payload, channel)
                elif event == "item_sold":
                    await on_item_sold(payload, channel)
                elif event == "item_listed" and with_probability(750):
                    await on_item_listed(payload, channel)
                await asyncio.sleep(stream_interval)
    except Exception as e:
        logger.error(f"Error in start_stream, will restart: {e}")
        await start_stream()


async def approve_erc20_allowance(
        ctx: SlashContext, tx_db: TxDB, asset_name, user_address, price, allowance, token_name, token_contract,
        token_id, next_tx_to, next_tx_data, next_tx_value, is_bid, highest_bid, expiration_time, asset_url, asset_img):
    try:
        tx_key = generate_tx_key()

        tx_db.add_tx({"tx_key": tx_key,
                      "user": ctx.author_id,
                      "action": "approve_erc20_allowance",
                      "channel": ctx.channel_id,
                      "next_action_data": json.dumps(
                          {"asset_name": asset_name,
                           "price": price,
                           "highest_bid": highest_bid,
                           "expiration_time": expiration_time,
                           "token_name": token_name,
                           "is_bid": is_bid,
                           "asset_url": asset_url,
                           "asset_img": asset_img,
                           "token_id": token_id,
                           "next_tx_to": next_tx_to,
                           "next_tx_from": user_address,
                           "next_tx_data": next_tx_data,
                           "next_tx_value": next_tx_value})})
        tx_data = token_contract.encodeABI(fn_name="approve", args=[opensea_conduit.address, price])

        tx = {
            "to": token_contract.address,
            "from": user_address,
            "data": compress_string_to_url(tx_data),
            "value": 0
        }

        tx_url = get_tx_page_url(tx_key, tx)

        user_balance = token_contract.functions.balanceOf(user_address).call()

        token_symbol = token_name.upper()
        embed = Embed(
            title=f"Approve OpenSea to transfer {token_symbol}",
            description=f"In order to approve `{token_symbol}` transfers, {tx_link_instruction_text}",
            fields=[
                {"name": "Asset Name",
                 "value": f"[{truncate_string(asset_name, 20)}]({asset_url})",
                 "inline": True},
                {"name": "Price",
                 "value": f"{format_wei_price(price, token_name)}",
                 "inline": True},
                {"name": "Current Allowance",
                 "value": format_wei_price(allowance, token_name),
                 "inline": True},
                {"name": "Current Balance",
                 "value": format_wei_price(user_balance, token_name),
                 "inline": True}],
            thumbnail=asset_img,
            color=BrandColors.GREEN,
            url=tx_url)

        return await ctx.send(
            f"To proceed with the purchase of `{asset_name}`, we first need your authorization to allow OpenSea to "
            f"transfer `{token_symbol}` from your Ethereum address.",
            embeds=embed, ephemeral=True)
    except Exception as e:
        await ctx.send(f"I'm sorry, but an error occurred while trying to approve transactions for `{token_symbol}`.",
                       ephemeral=True)
        logger.error(f"approve_erc20 exception {str(e)}")
        raise e


async def send_buy_tx_url(
        ctx: SlashContext, tx_db: TxDB, web3, asset_name, price, token_name, tx_to, tx_from, tx_data, tx_value,
        token_id, ctx_author_id, ctx_channel_id, expiration_time, highest_bid, is_bid, asset_url):
    try:
        tx = {
            "to": tx_to,
            "from": tx_from,
            "data": compress_string_to_url(tx_data),
            "value": tx_value
        }
        tx_key = generate_tx_key()
        tx_url = get_tx_page_url(tx_key, tx, sign_spec="OrderComponents" if is_bid else None)
        formatted_price = format_wei_price(price, token_name)
        expiration_datetime = datetime.fromtimestamp(expiration_time)
        formatted_expiration = format_datetime(expiration_datetime)

        tx_db.add_tx(
            {"tx_key": tx_key,
             "user": ctx_author_id,
             "action": "bid" if is_bid else "buy",
             "channel": ctx_channel_id,
             "next_action_data": json.dumps(
                 {"asset_name": asset_name,
                  "formatted_price": formatted_price,
                  "token_name": token_name,
                  "expiration_time": expiration_time,
                  "token_id": token_id,
                  "order_params": tx_data if is_bid else ""})})

        _, token_contract = get_token_contract(web3, get_token_address(token_name))
        if token_contract is None:
            user_balance = web3.eth.get_balance(tx_from)
        else:
            user_balance = token_contract.functions.balanceOf(tx_from).call()

        if is_bid:
            embed = Embed(
                title=f"Make `{formatted_price}` offer for the {asset_name}",
                description=f"In order to make an offer for the `{asset_name}`, {tx_link_instruction_text}",
                fields=[{"name": "Asset Name", "value": f"[{asset_name}]({asset_url})", "inline": True},
                        {"name": "Your Offer", "value": f"{formatted_price}", "inline": True},
                        {"name": "Highest Offer",
                         "value": "None" if highest_bid is None else format_wei_price(highest_bid, token_name),
                         "inline": True},
                        {"name": "Current Balance",
                         "value": format_wei_price(user_balance, token_name), "inline": True},
                        {"name": "Expiration", "value": formatted_expiration}],
                color=BrandColors.GREEN,
                url=tx_url)
            return await ctx.send(
                f"Get ready to make an offer for `{asset_name}`! Don't wait too long, as it's ending "
                f"{format_time_remaining(expiration_datetime)}!",
                embeds=embed, ephemeral=True)
        else:
            embed = Embed(
                title=f"Buy `{asset_name}`",
                description=f"In order to purchase `{asset_name}`, {tx_link_instruction_text}",
                fields=[{"name": "Asset Name", "value": f"[{asset_name}]({asset_url})", "inline": True},
                        {"name": "Price", "value": f"{formatted_price}", "inline": True},
                        {"name": "Current Balance",
                         "value": format_wei_price(user_balance, token_name),
                         "inline": True},
                        {"name": "Offer Ends", "value": formatted_expiration, "inline": True}],
                color=BrandColors.GREEN,
                url=tx_url)
            return await ctx.send(f"Lucky day! `{asset_name}` can be purchased instantly! Don't wait too long, "
                                  f"as it's ending {format_time_remaining(expiration_datetime)}!",
                                  embeds=embed, ephemeral=True)


    except Exception as e:
        await ctx.send(f"I'm sorry, but an error occurred while trying to initiate the purchase of `{asset_name}`.",
                       ephemeral=True)
        logger.error(f"send_buy_tx_url exception {str(e)}")
        raise e


async def buy(
        ctx, web3, user_address_db: UserAddressDB, tx_db: TxDB, token_id, asset_type: AssetType, asset_name,
        bid=None, currency=None, force_bid=False):
    try:
        eth_usd_price = await get_eth_usd_price()
        bid_wei = None
        highest_bid = None
        is_bid = False

        user_address = user_address_db.get_address(ctx.author_id)

        if user_address is None:
            return await discord_web3.link_wallet(
                ctx=ctx,
                tx_db=tx_db,
                message="To be able to purchase from OpenSea, we need your Ethereum address associated "
                        "with your Discord account.")

        cheapest_listing = await get_cheapest_listing(asset_contract_address, token_id)

        if (not bid or not currency) and not cheapest_listing:
            highest_bid, highest_bid_token = await get_highest_bid(asset_contract_address, token_id)
            if highest_bid:
                highest_bid_token_name, _ = get_token_contract(highest_bid_token)
                highest_bid_msg = f"Current highest bid is `{format_wei_price(highest_bid, highest_bid_token_name)}`."
            else:
                highest_bid_msg = "Currently there are no other offers for this NFT."

            components = None
            if highest_bid:
                components = Button(
                    style=ButtonStyle.GRAY,
                    label=f"Make Better Offer",
                    emoji="☝",
                    custom_id=f"offer_btn_0_{highest_bid_token_name}_{get_button_id(asset_name, token_id)}")

            return await ctx.send(f"It appears that `{asset_name}` is not currently listed for sale on OpenSea. "
                                  f"You can still make an offer for this name by specifying `bid` and `currency` "
                                  f"parameters. {highest_bid_msg}",
                                  ephemeral=True, components=components)

        if currency:
            cons_token = get_token_address(currency)
            cons_token_name, cons_token_contract = get_token_contract(cons_token)
        else:
            cons_token = cheapest_listing["protocol_data"]["parameters"]["consideration"][0]["token"]
            cons_token_name, cons_token_contract = get_token_contract(cons_token)

        if (not bid or not currency) and cheapest_listing:
            current_price = int(cheapest_listing["current_price"])
            order_type = cheapest_listing["order_type"]
            offerer = cheapest_listing["protocol_data"]["parameters"]["offerer"]
            expiration_time = cheapest_listing["expiration_time"]
        else:
            current_price = 0
            order_type = None
            offerer = ""
            expiration_time = int((datetime.now() + timedelta(days=100)).timestamp())

        if user_address.lower() == offerer.lower():
            return await ctx.send(f"It seems that your linked Ethereum address has listed `{cured_name}` on OpenSea.",
                                  ephemeral=True)

        if bid is None and cheapest_listing:
            order_hash = cheapest_listing["order_hash"]
            protocol_address = cheapest_listing["protocol_address"]
            fulfillment = await get_fulfillment(order_hash, protocol_address, user_address)
            if not "fulfillment_data" in fulfillment:
                return await ctx.send(
                    f"It seems like OpenSea is still preparing this NFT, please try again in a few seconds",
                    ephemeral=True)
        else:
            bid = restrict_to_multiples(Decimal(str(bid)))
            is_worth, usd_worth = min_usd_worth(bid, 5, eth_usd_price, cons_token_name)

            if not is_worth:  # user specified bid is not 5 USD worth
                if force_bid:
                    bid_wei = calculate_bid(0, None, None, cons_token_name, eth_usd_price)
                else:
                    return await ctx.send(
                        f"Apologies, but the bid must be worth more than 5 USD per unit. "
                        f"Got {round(usd_worth, 2)} USD per unit", ephemeral=True)
            else:
                bid_wei = safe_to_wei(bid, cons_token_name)

        if not bid_wei and (order_type == "basic" or order_type == "dutch"):
            tx_data, tx_value, tx_to = get_tx_data_from_fulfillment(fulfillment, order_type)
        elif bid_wei or order_type == "english":
            is_bid = True
            highest_bid, _ = await get_highest_bid(asset_contract_address, token_id)
            current_price = calculate_bid(current_price, bid_wei, highest_bid, cons_token_name, eth_usd_price,
                                          force_highest_bid=force_bid)

            if current_price == "too_low":
                return await ctx.send(
                    f"Apologies, but your bid is not higher than the current highest bid of "
                    f"`{format_wei_price(highest_bid, cons_token_name)}`.",
                    ephemeral=True)

            (_, _, os_fee_start_price, _) = get_order_prices(current_price, unit="wei")
            order_params = get_order_parameters(
                offerer=user_address,
                offer_asset_type=1,
                offer_token=cons_token,
                offer_token_id=0,
                offer_start_amount=current_price,
                offer_end_amount=current_price,
                cons_asset_type=asset_type,
                cons_token=asset_contract_address,
                cons_token_id=token_id,
                cons_start_amount=1,
                cons_end_amount=1,
                cons_recepient=user_address,
                os_cons_asset_type=1,
                os_cons_token=cons_token,
                os_cons_start_amount=os_fee_start_price,
                os_cons_end_amount=os_fee_start_price,
                start_time=int(datetime.now().timestamp()),
                end_time=int(expiration_time) + 604800,  # + 1 week
                order_type=0)
            tx_data = json.dumps(order_params)
            tx_value = 0

        if cons_token_name != "eth":
            allowance = int(cons_token_contract.functions.allowance(user_address, opensea_conduit.address).call())

            if allowance < current_price:
                return await approve_erc20_allowance(
                    ctx=ctx,
                    tx_db=tx_db,
                    asset_name=cured_name,
                    user_address=user_address,
                    price=current_price,
                    allowance=allowance,
                    token_name=cons_token_name,
                    token_contract=cons_token_contract,
                    expiration_time=expiration_time,
                    highest_bid=highest_bid,
                    is_bid=is_bid,
                    asset_url=get_asset_url(asset_contract_address, token_id),
                    token_id=token_id,
                    next_tx_to=contract_addresses["Seaport"],
                    next_tx_data=tx_data,
                    next_tx_value=tx_value)

        return await send_buy_tx_url(
            ctx=ctx,
            web3=web3,
            tx_db=tx_db,
            asset_name=cured_name,
            price=current_price,
            is_bid=is_bid,
            token_name=cons_token_name,
            expiration_time=expiration_time,
            highest_bid=highest_bid,
            asset_url=get_asset_url(asset_contract_address, token_id),
            token_id=token_id,
            tx_to=contract_addresses["Seaport"],
            tx_from=user_address,
            tx_data=tx_data,
            tx_value=tx_value,
            ctx_author_id=ctx.author_id,
            ctx_channel_id=ctx.channel_id)

    except Exception as e:
        await ctx.send(f"I'm sorry, but an error occurred while trying to initiate the purchase of `{asset_name}`.",
                       ephemeral=True)
        logger.error(f"Buy exception {str(e)}")
        raise e


async def on_accept_offer_btn(ctx, tx_db: TxDB, paginator, offers):
    offer = offers[paginator.page_index]
    fulfillment = await get_fulfillment(offer["order_hash"], offer["protocol_address"], offer["user_address"])
    tx_data, tx_value, tx_to = get_tx_data_from_fulfillment(fulfillment, offer["order_type"])

    tx = {
        "to": tx_to,
        "from": offer["user_address"],
        "data": compress_string_to_url(tx_data),
        "value": tx_value
    }
    tx_key = generate_tx_key()
    tx_url = get_tx_page_url(tx_key, tx)

    tx_db.add_tx(
        {"tx_key": tx_key,
         "user": ctx.author_id,
         "action": "accept_offer",
         "channel": ctx.channel_id,
         "next_action_data": json.dumps(offer)})

    embed = Embed(
        title=f"Accept an offer for `{offer['asset_name']}`",
        description=f"In order to accept this offer, {tx_link_instruction_text}",
        fields=offer["fields"],
        color=BrandColors.GREEN,
        thumbnail=offer['asset_img'],
        url=tx_url)

    return await ctx.send(f"You're about to accept the following offer for your NFT:", embeds=embed, ephemeral=True)


async def offers(
        ctx: SlashContext, user_address_db: UserAddressDB, tx_db: TxDB, asset_contract_address, token_id, asset_name):
    try:
        user_address = user_address_db.get_address(ctx.author_id)

        offers = await get_offers(asset_contract_address, token_id)
        asset_url = get_asset_url(asset_contract_address, token_id)

        if not "orders" in offers or len(offers["orders"]) == 0:
            return await ctx.send(f"It seems that no offers are present for this NFT.", ephemeral=True)

        embeds = []
        offers_data = []
        for i, offer in enumerate(offers["orders"]):
            created = format_datetime(datetime.fromtimestamp(offer["listing_time"]))
            expiration = format_datetime(datetime.fromtimestamp(offer["expiration_time"]))
            offerer = offer.get("maker", {}).get("address", "")
            offer_token = offer.get("protocol_data", {}).get("parameters", {}).get("offer", [])[0].get("token", "")
            offer_token_name, _ = get_token_contract(offer_token)
            price = int(offer["current_price"])
            maker_img_url = offer.get("maker", {}).get("profile_img_url", "")
            eth_usd_price = await get_eth_usd_price()
            usd_price = get_usd_price(safe_to_ether(price, offer_token_name), eth_usd_price, offer_token_name)
            formatted_price = format_wei_price(price, offer_token_name)

            fields = [
                {"name": "Asset Name", "value": f"[{asset_name}]({asset_url})", "inline": True},
                {"name": "Offerer", "value": format_user_address_url(offerer), "inline": True},
                {"name": "Created", "value": created, "inline": True},
                {"name": "Expiration", "value": expiration, "inline": True},
                {"name": "Price", "value": formatted_price, "inline": True},
                {"name": "USD Value", "value": format_eth_price(usd_price, "USD", 2), "inline": True}
            ]
            embed = Embed(
                title=f"Offer #{i + 1}",
                fields=fields,
                color=BrandColors.WHITE,
                thumbnail=maker_img_url)
            embeds.append(embed)
            offers_data.append(
                {"asset_name": asset_name,
                 "order_hash": offer["order_hash"],
                 "protocol_address": offer["protocol_address"],
                 "order_type": offer["order_type"],
                 "user_address": user_address,
                 "formatted_price": formatted_price,
                 "fields": fields})

        paginator = Paginator.create_from_embeds(bot, *embeds)
        paginator.default_button_color = ButtonStyle.GRAY

        owner, _, _ = await get_name_owner(cured_name)

        if owner.lower() == user_address.lower():
            paginator.show_callback_button = True

        paginator.callback = partial(on_accept_offer_btn, tx_db=tx_db, paginator=paginator, offers=offers_data)
        return await paginator.send(ctx)

    except Exception as e:
        await ctx.send(f"I'm sorry, but an error occurred while trying to initiate the purchase of `{asset_name}`.",
                       ephemeral=True)
        logger.error(f"Buy command exception {str(e)}")
        raise e


async def approve_opensea(ctx: SlashContext, tx_db: TxDB, user_address, asset_contract, asset_name, token_id,
                          is_wrapped, start_price, end_price, duration_days, currency):
    tx_key = generate_tx_key()

    tx_db.add_tx(
        {"tx_key": tx_key,
         "user": ctx.author_id,
         "action": "approve_opensea",
         "channel": ctx.channel_id,
         "next_action_data": json.dumps(
             {"asset_name": asset_name,
              "user_address": user_address,
              "start_price": start_price,
              "end_price": end_price,
              "duration_days": duration_days,
              "currency": currency,
              "token_id": token_id,
              "asset_contract_address": asset_contract.address,
              "is_wrapped": is_wrapped})})

    tx_data = asset_contract.encodeABI(fn_name="setApprovalForAll", args=[opensea_conduit.address, True])

    tx = {
        "to": asset_contract.address,
        "from": user_address,
        "data": compress_string_to_url(tx_data),
        "value": 0
    }

    tx_url = get_tx_page_url(tx_key, tx)

    embed = Embed(
        title=f"Approve OpenSea to transfer your NFTs",
        description=f"In order to approve OpenSea for NFT transfers, {tx_link_instruction_text}",
        color=BrandColors.GREEN,
        url=tx_url)

    return await ctx.send(
        "In order to sell this NFT, you will need to perform the following approval transaction for OpenSea.",
        embeds=embed, ephemeral=True)


async def sell(ctx: SlashContext, user_address_db: UserAddressDB, tx_db: TxDB, asset_name, asset_type: AssetType,
               token_id, asset_contract, start_price, end_price=None, duration_days=100, currency="eth"):
    try:
        user_address = user_address_db.get_address(ctx.author_id)
        if user_address is None:
            return await discord_web3.link_wallet(
                ctx=ctx,
                tx_db=tx_db,
                message="In order to facilitate the sale of your NFT, we need your "
                        "Ethereum address associated with your Discord account.")

        if end_price is None:
            end_price = start_price

        is_approved = asset_contract.functions.isApprovedForAll(
            user_address, contract_addresses["OpenSeaConduit"]).call()

        if not is_approved:
            return await approve_opensea(
                ctx=ctx,
                tx_db=tx_db,
                user_address=user_address,
                asset_name=asset_name,
                token_id=token_id,
                asset_type=asset_type,
                asset_contract=asset_contract,
                start_price=start_price,
                end_price=end_price,
                duration_days=duration_days,
                currency=currency)

        return await send_sell_sign_url(
            ctx=ctx,
            tx_db=tx_db,
            user_address=user_address,
            asset_name=asset_name,
            token_id=token_id,
            asset_type=asset_type,
            start_price=start_price,
            end_price=end_price,
            duration_days=duration_days,
            currency=currency,
            asset_contract_address=asset_contract.address,
            ctx_author_id=ctx.author_id,
            ctx_channel_id=ctx.channel_id)
    except DisallowedNameError as e:
        await send_invalid_name_msg(ctx, asset_name)
    except Exception as e:
        await ctx.send(f"I'm sorry, but an error occurred while trying to initiate the selling of `{cured_name}`.",
                       ephemeral=True)
        logger.error(f"Sell command exception {str(e)}")
        raise e


async def send_sell_sign_url(
        ctx, tx_db: TxDB, token_id, asset_name, user_address, asset_type: AssetType, start_price, end_price,
        duration_days, currency, asset_contract_address, ctx_author_id, ctx_channel_id, ctx_message=""):
    start_time, end_time = get_order_start_end_times(duration_days)
    owner_start_price, owner_end_price, os_fee_start_price, os_fee_end_price = \
        get_order_prices(start_price, end_price, token_name=currency)
    cons_token_address = get_token_address(currency)

    order_params = get_order_parameters(
        offerer=user_address,
        offer_item_type=asset_type,
        offer_token=asset_contract_address,
        offer_token_id=token_id,
        offer_start_amount=1,
        offer_end_amount=1,
        cons_item_type=0 if currency == "eth" else 1,
        cons_token=cons_token_address,
        cons_token_id=0,
        cons_start_amount=owner_start_price,
        cons_end_amount=owner_end_price,
        cons_recepient=user_address,
        os_cons_item_type=0 if currency == "eth" else 1,
        os_cons_token=cons_token_address,
        os_cons_start_amount=os_fee_start_price,
        os_cons_end_amount=os_fee_end_price,
        start_time=start_time,
        end_time=end_time,
        order_type=1)

    order_params_json = json.dumps(order_params)

    tx = {
        "to": contract_addresses["Seaport"],
        "from": user_address,
        "data": compress_string_to_url(order_params_json),
        "value": 0
    }

    tx_key = generate_tx_key()
    tx_url = get_tx_page_url(tx_key, tx, sign_spec="OrderComponents")

    tx_db.add_tx(
        {"tx_key": tx_key,
         "user": ctx_author_id,
         "action": "sell",
         "channel": ctx_channel_id,
         "next_action_data": order_params_json})

    expiration_datetime = datetime.fromtimestamp(end_time)

    embed = Embed(
        title=f"Sign the sales contract for `{asset_name}`",
        description=f"To initiate the selling process for `{asset_name}`, {tx_link_instruction_text}",
        fields=[{"name": "ENS Name", "value": f"[{asset_name}]({get_asset_url(asset_contract_address, token_id)})",
                 "inline": True},
                {"name": "Start Price", "value": format_eth_price(start_price, currency), "inline": True},
                {"name": "End Price", "value": format_eth_price(end_price, currency), "inline": True},
                {"name": "OpenSea Fee", "value": "2.5%", "inline": True},
                {"name": "Duration", "value": f"{duration_days} days", "inline": True},
                {"name": "Expiration", "value": format_datetime(expiration_datetime), "inline": True}],
        color=BrandColors.GREEN,
        url=tx_url)

    return await ctx.send(ctx_message, embeds=embed, ephemeral=True)


async def bid_callback(bot, tx, tx_signature, next_action_data):
    try:
        channel = bot.get_channel(int(tx["channel"]))
        user = bot.get_user(int(tx["user"]))

        ctx_author = tx["user"]

        order_params = {
            "parameters": json.loads(next_action_data["order_params"]),
            "signature": tx_signature,
            "protocol_address": contract_addresses["Seaport"]
        }

        response = await http_client.post(opensea_urls["offers"], json=order_params, headers=os_api_headers)
        response = response.json()

        if "errors" in response:
            await user.send(
                f"Apologies, an error occurred while attempting to send your order to OpenSea.\n"
                f"```{response['errors'][0]}```")
            return

        asset_name = next_action_data["asset_name"]
        formatted_price = next_action_data["formatted_price"]
        token_name = next_action_data["token_name"]
        expiration_datetime = datetime.fromtimestamp(next_action_data["expiration_time"])

        components = Button(
            style=ButtonStyle.GRAY,
            label=f"Make Better Offer",
            emoji="☝",
            custom_id=f"offer_btn_0_weth_{next_action_data['button_id']}",
        )

        return await channel.send(
            f"Exciting news! {user.mention} has just placed the highest bid of `{formatted_price}` "
            f"for the name `{asset_name}`! Don't miss out and place your bid before the offer ends "
            f"{format_time_remaining(expiration_datetime)}!", components=components)
    except Exception as e:
        logger.error(f"Error in bid_callback {str(e)}")
        raise e


async def buy_callback(web3, tx, tx_hash, next_action_data):
    try:
        await discord_web3.wait_for_receipt(tx_hash)
        channel = bot.get_channel(int(tx["channel"]))
        asset_name = next_action_data["asset_name"]
        formatted_price = next_action_data["formatted_price"]
        ctx_author = tx["user"]

        components = Button(
            style=ButtonStyle.GRAY,
            label=f"Make First Offer",
            emoji="☝",
            custom_id=f"offer_btn_0_weth_{next_action_data['button_id']}")

        return await channel.send(
            f"Exciting announcement! `{asset_name}` has been successfully purchased by {mention(ctx_author)} "
            f"for `{formatted_price}`!", components=components)
    except Exception as e:
        logger.error(f"Error in buy_callback {str(e)}")
        raise e


async def sell_callback(bot, web3, tx, tx_signature, next_action_data):
    try:
        channel = bot.get_channel(int(tx["channel"]))
        user = bot.get_user(int(tx["user"]))

        ctx_author = tx["user"]

        order_params = {
            "parameters": next_action_data,
            "signature": tx_signature,
            "protocol_address": contract_addresses["Seaport"]
        }

        (cons_token_name, _) = get_token_contract(web3, next_action_data["consideration"][0]["token"])

        response = await http_client.post(opensea_urls['listings'], json=order_params, headers=os_api_headers)
        response = response.json()

        if "errors" in response:
            await user.send(
                f"Apologies, an error occurred while attempting to send your order to OpenSea.\n"
                f"```{response['errors'][0]}```")
            return

        order = response["order"]
        asset = order["maker_asset_bundle"]["assets"][0]
        token_id = asset["token_id"]
        contract_address = asset["asset_contract"]["address"]
        asset_name = asset["name"]
        expiration_date = datetime.fromtimestamp(order["expiration_time"])

        formatted_price = format_wei_price(order["current_price"], cons_token_name)

        display_asset_name = asset_name
        if asset_name is None:
            display_asset_name = f"#{str(token_id)[:10]}"

        asset_url = get_asset_url(contract_address, token_id)

        embed = Embed(
            title=f"Our friend {user.display_name} has just listed {display_asset_name} for sale!",
            fields=[
                {"name": "Asset name", "value": f"[{display_asset_name}]({asset_url})", "inline": True},
                {"name": "Offerer", "value": user.mention, "inline": True},
                {"name": "Price", "value": formatted_price, "inline": True},
                {"name": "Expiration", "value": format_datetime(expiration_date), "inline": True}
            ],
            color=BrandColors.BLURPLE)

        components = Button(
            style=ButtonStyle.GRAY,
            label=f"Buy",
            emoji="☝",
            custom_id=f"buy_btn_{get_button_id(asset_name, token_id)}")

        return await channel.send(f"{user.mention} is selling `{display_asset_name}`!", embeds=embed,
                                  components=components)
    except Exception as e:
        logger.error(f"Error in sell_callback {str(e)}")
        raise e


async def approve_opensea_callback(bot, web3, tx_db: TxDB, tx, tx_hash, next_action_data):
    try:
        await discord_web3.wait_for_receipt(web3, tx_hash)
        user = bot.get_user(int(tx["user"]))
        message = f"With the approval complete, we can now proceed to sell `{next_action_data['asset_name']}`."
        return await send_sell_sign_url(
            ctx=user,
            tx_db=tx_db,
            start_price=next_action_data["start_price"],
            end_price=next_action_data["end_price"],
            duration_days=next_action_data["duration_days"],
            currency=next_action_data["currency"],
            user_address=next_action_data["user_address"],
            asset_name=next_action_data["asset_name"],
            token_id=next_action_data["token_id"],
            asset_contract_address=next_action_data["assset_contract_address"],
            asset_type=next_action_data["asset_type"],
            ctx_author_id=tx["user"],
            ctx_channel_id=tx["channel"],
            ctx_message=message)
    except Exception as e:
        logger.error(f"Error in approve_opensea_callback {str(e)}")
        raise e


async def approve_erc20_allowance_callback(bot, web3, tx_db: TxDB, tx, tx_hash, next_action_data):
    try:
        await discord_web3.wait_for_receipt(web3, tx_hash)
        user = bot.get_user(int(tx["user"]))
        return await send_buy_tx_url(
            ctx=user,
            web3=web3,
            tx_db=tx_db,
            asset_name=next_action_data["asset_name"],
            price=next_action_data["price"],
            token_name=next_action_data["token_name"],
            tx_to=next_action_data["next_tx_to"],
            tx_from=next_action_data["next_tx_from"],
            tx_data=next_action_data["next_tx_data"],
            tx_value=next_action_data["next_tx_value"],
            is_bid=next_action_data["is_bid"],
            asset_url=next_action_data["asset_url"],
            token_id=next_action_data["token_id"],
            highest_bid=next_action_data["highest_bid"],
            expiration_time=next_action_data["expiration_time"],
            ctx_author_id=tx["user"],
            ctx_channel_id=tx["channel"])
    except Exception as e:
        logger.error(f"Error in approve_erc20_allowance_callback {str(e)}")
        raise e
