// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.24;

// Never deployed. Its RUNTIME bytecode is injected with an eth_call state override at a throwaway
// address that is also given an ETH balance, then `probe` is called. Nothing is broadcast.
//   forge build  ->  copy deployedBytecode.object to baselab/security/simulator.bin
// Missing simulator.bin => the validator fails closed (every token rejected).

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
    function transfer(address, uint256) external returns (bool);
}

interface IWETH is IERC20 {
    function deposit() external payable;
}

interface IRouterV2 {
    function getAmountsOut(uint256 amountIn, address[] calldata path) external view returns (uint256[] memory);
    function swapExactTokensForTokensSupportingFeeOnTransferTokens(
        uint256 amountIn, uint256 amountOutMin, address[] calldata path, address to, uint256 deadline) external;
}

contract HoneypotSimulator {
    struct Result {
        bool buyOk;
        bool transferOk;
        bool sellOk;
        uint256 expectedBuy;
        uint256 receivedBuy;
        uint256 expectedSell;
        uint256 receivedSell;
    }

    function probe(address router, address weth, address token, uint256 amountIn)
        external
        returns (Result memory r)
    {
        IWETH(weth).deposit{value: amountIn}();
        IERC20(weth).approve(router, amountIn);

        address[] memory buyPath = new address[](2);
        buyPath[0] = weth;
        buyPath[1] = token;
        r.expectedBuy = IRouterV2(router).getAmountsOut(amountIn, buyPath)[1];

        uint256 before = IERC20(token).balanceOf(address(this));
        try IRouterV2(router).swapExactTokensForTokensSupportingFeeOnTransferTokens(
            amountIn, 0, buyPath, address(this), block.timestamp) {
            r.buyOk = true;
        } catch {
            return r;
        }
        r.receivedBuy = IERC20(token).balanceOf(address(this)) - before;
        if (r.receivedBuy == 0) return r;

        // plain transfer leg (wallet-to-wallet): many honeypots only block this path
        uint256 probeAmt = r.receivedBuy / 1000;
        if (probeAmt > 0) {
            try IERC20(token).transfer(address(uint160(uint256(keccak256("probe-sink")))), probeAmt) returns (bool ok) {
                r.transferOk = ok;
            } catch {}
        } else {
            r.transferOk = true;
        }

        uint256 sellAmt = IERC20(token).balanceOf(address(this));
        address[] memory sellPath = new address[](2);
        sellPath[0] = token;
        sellPath[1] = weth;
        try IRouterV2(router).getAmountsOut(sellAmt, sellPath) returns (uint256[] memory q) {
            r.expectedSell = q[1];
        } catch {
            return r;
        }
        IERC20(token).approve(router, sellAmt);
        uint256 wBefore = IERC20(weth).balanceOf(address(this));
        try IRouterV2(router).swapExactTokensForTokensSupportingFeeOnTransferTokens(
            sellAmt, 0, sellPath, address(this), block.timestamp) {
            r.sellOk = true;
            r.receivedSell = IERC20(weth).balanceOf(address(this)) - wBefore;
        } catch {}
    }
}
