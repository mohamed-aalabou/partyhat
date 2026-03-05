// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC20Burnable} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Burnable.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/// @title FourFourToken
/// @notice Fixed-supply, burnable, pausable ERC-20 token for the four-four project.
contract FourFourToken is ERC20, ERC20Burnable, Pausable, Ownable {
    /// @dev Fixed supply: 1,000,000,000 tokens (using the default 18 decimals).
    uint256 private constant FIXED_SUPPLY = 1_000_000_000 * 10 ** 18;

    /// @param name_ Token name.
    /// @param symbol_ Token symbol.
    /// @dev Mints the full fixed supply to the deployer and sets deployer as owner.
    constructor(string memory name_, string memory symbol_) ERC20(name_, symbol_) Ownable(msg.sender) {
        _mint(msg.sender, FIXED_SUPPLY);
    }

    /// @notice Pauses all token transfers.
    /// @dev Only callable by the owner.
    function pause() external onlyOwner {
        _pause();
    }

    /// @notice Unpauses token transfers.
    /// @dev Only callable by the owner.
    function unpause() external onlyOwner {
        _unpause();
    }

    /// @dev Enforces pause on transfers, mints, and burns.
    /// Uses OZ v5 hook (_update). Inherits EnforcedPause() error from Pausable.
    function _update(address from, address to, uint256 value) internal override {
        if (paused()) revert EnforcedPause();
        super._update(from, to, value);
    }
}
