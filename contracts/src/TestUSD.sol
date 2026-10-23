// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @dev Local-only test asset. Never used as a production currency.
contract TestUSD is ERC20 {
    constructor() ERC20("AI Mining Network Test USD", "TESTUSD") {}
    function mint(address recipient, uint256 amount) external { _mint(recipient, amount); }
}
