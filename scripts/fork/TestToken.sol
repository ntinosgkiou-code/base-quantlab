// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.24;

// Fork-test fixture ONLY (local anvil fork). Configurable buy/sell tax and a honeypot switch that
// blocks every sell into the pair except by the deployer, so the validator can be proven to catch them.
contract TestToken {
    string public name;
    string public symbol;
    uint8 public constant decimals = 18;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    address public owner;
    address public immutable deployer;
    address public pair;
    uint256 public immutable buyTaxBps;
    uint256 public immutable sellTaxBps;
    bool public immutable honeypot;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor(string memory n, uint256 buyBps, uint256 sellBps, bool hp) {
        name = n;
        symbol = n;
        owner = msg.sender;
        deployer = msg.sender;
        buyTaxBps = buyBps;
        sellTaxBps = sellBps;
        honeypot = hp;
        totalSupply = 1e27;
        balanceOf[msg.sender] = 1e27;
        emit Transfer(address(0), msg.sender, 1e27);
    }

    function setPair(address p) external {
        require(msg.sender == deployer, "auth");
        pair = p;
    }

    function renounceOwnership() external {
        require(msg.sender == owner, "auth");
        owner = address(0);
    }

    function airdrop(address[] calldata to, uint256 amount) external {
        for (uint256 i = 0; i < to.length; i++) _transfer(msg.sender, to[i], amount);
    }

    function approve(address s, uint256 a) external returns (bool) {
        allowance[msg.sender][s] = a;
        emit Approval(msg.sender, s, a);
        return true;
    }

    function transfer(address to, uint256 a) external returns (bool) {
        _transfer(msg.sender, to, a);
        return true;
    }

    function transferFrom(address from, address to, uint256 a) external returns (bool) {
        uint256 al = allowance[from][msg.sender];
        if (al != type(uint256).max) allowance[from][msg.sender] = al - a;
        _transfer(from, to, a);
        return true;
    }

    function _transfer(address from, address to, uint256 a) internal {
        if (honeypot && to == pair && from != deployer) revert("sell blocked");
        uint256 tax;
        if (from != deployer && to != deployer) {
            if (from == pair) tax = a * buyTaxBps / 10_000;
            else if (to == pair) tax = a * sellTaxBps / 10_000;
        }
        balanceOf[from] -= a;
        balanceOf[to] += a - tax;
        emit Transfer(from, to, a - tax);
        if (tax > 0) {
            balanceOf[deployer] += tax;
            emit Transfer(from, deployer, tax);
        }
    }
}
